//! jam080-build — build a JAM service (or authorizer) crate into a GP 0.8.0 `.jam` blob.
//!
//! The published `jam-pvm-build` 0.1.28 targets GP 0.7.2: it links with polkavm-linker
//! 0.30, whose `JamV1` ISA still has `sbrk`, and its SDK uses the 0.7.2 host-call ids.
//! This is the same pipeline (same toolchain, rustflags and cargo invocation as
//! jam-pvm-builder 0.1.28, Apache-2.0) on polkavm-linker 0.36, whose `JamV1` is the GP
//! 0.8.0 ISA (no `sbrk`, `unlikely` = opcode 2). Pair it with the patched SDK in
//! `../jam-pvm-common` (0.8.0 host-call ids + a `grow_heap` allocator).
//!
//! Usage: jam080-build <crate-dir> [--out <dir>] [--authorizer]
//! Writes <out>/<crate>.jam and prints its blake2b-256 code hash.

use codec::Encode;
use jam_program_blob_common::{ConventionalMetadata, CrateInfo, ProgramBlob};
use std::path::{Path, PathBuf};
use std::process::Command;

const TOOLCHAIN: &str = "nightly-2025-05-10";
const TARGET_NAME: &str = "riscv64emac-unknown-none-polkavm";

fn die(msg: impl std::fmt::Display) -> ! {
    eprintln!("jam080-build: {msg}");
    std::process::exit(1)
}

fn crate_info(dir: &Path) -> CrateInfo {
    let out = Command::new("cargo")
        .current_dir(dir)
        .args(["metadata", "--no-deps", "--format-version", "1"])
        .output()
        .unwrap_or_else(|e| die(format!("cargo metadata: {e}")));
    if !out.status.success() {
        die(format!("cargo metadata failed: {}", String::from_utf8_lossy(&out.stderr)));
    }
    let meta: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap_or_else(|e| die(e));
    let manifest = dir.join("Cargo.toml").canonicalize().unwrap_or_else(|e| die(e));
    let pkg = meta["packages"]
        .as_array()
        .and_then(|ps| {
            ps.iter().find(|p| {
                p["manifest_path"].as_str().map(|m| Path::new(m) == manifest).unwrap_or(false)
            })
        })
        .unwrap_or_else(|| die("package not found in cargo metadata"));
    let s = |k: &str| pkg[k].as_str().unwrap_or("").to_string();
    CrateInfo {
        name: s("name"),
        version: s("version"),
        license: s("license"),
        authors: pkg["authors"]
            .as_array()
            .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
            .unwrap_or_default(),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut crate_dir: Option<PathBuf> = None;
    let mut out_dir: Option<PathBuf> = None;
    let mut authorizer = false;
    let mut it = args.into_iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--out" => out_dir = Some(PathBuf::from(it.next().unwrap_or_else(|| die("--out needs a dir")))),
            "--authorizer" => authorizer = true,
            _ if crate_dir.is_none() => crate_dir = Some(PathBuf::from(a)),
            _ => die(format!("unexpected argument {a}")),
        }
    }
    let crate_dir = crate_dir.unwrap_or_else(|| die("usage: jam080-build <crate-dir> [--out <dir>] [--authorizer]"));
    let crate_dir = crate_dir.canonicalize().unwrap_or_else(|e| die(e));
    let out_dir = out_dir.unwrap_or_else(|| crate_dir.clone());
    let target_dir = crate_dir.join("target").join("jam080");
    let info = crate_info(&crate_dir);

    // Same target JSON choice as jam-pvm-builder 0.1.28 (pinned nightly => Legacy).
    let mut targs = polkavm_linker::TargetJsonArgs::default();
    targs.is_64_bit = true;
    targs.rustc_version = polkavm_linker::RustcVersion::Legacy;
    let target_json = polkavm_linker::target_json_path(targs).unwrap_or_else(|e| die(e));

    let status = Command::new("cargo")
        .current_dir(&crate_dir)
        .env_clear()
        .env("PATH", std::env::var("PATH").unwrap_or_default())
        .env("HOME", std::env::var("HOME").unwrap_or_default())
        .env("CARGO_ENCODED_RUSTFLAGS", "-C\x1fpanic=abort")
        .env("CARGO_TARGET_DIR", &target_dir)
        .env("RUSTC_BOOTSTRAP", "1")
        .arg(format!("+{TOOLCHAIN}"))
        .args(["rustc", "--lib", "--crate-type=cdylib", "-Z", "build-std=core,alloc",
               "-Z", "build-std-features=panic_immediate_abort", "--release", "--target"])
        .arg(&target_json)
        .status()
        .unwrap_or_else(|e| die(format!("cargo: {e}")));
    if !status.success() {
        die("cargo build of the PVM crate failed");
    }

    let elf = target_dir
        .join(TARGET_NAME)
        .join("release")
        .join(format!("{}.elf", info.name.replace('-', "_")));
    let orig = std::fs::read(&elf).unwrap_or_else(|e| die(format!("{}: {e}", elf.display())));

    let mut config = polkavm_linker::Config::default();
    config.set_strip(true);
    let exports: Vec<Vec<u8>> = if authorizer {
        vec![b"is_authorized_ext".to_vec()]
    } else {
        vec![b"refine_ext".to_vec(), b"accumulate_ext".to_vec()]
    };
    config.set_dispatch_table(exports);
    let linked = polkavm_linker::program_from_elf(config, polkavm_linker::TargetInstructionSet::JamV1, &orig)
        .unwrap_or_else(|e| die(format!("link: {e}")));
    let parts = polkavm_linker::ProgramParts::from_bytes(linked.into()).unwrap_or_else(|e| die(format!("parts: {e}")));

    // jam-program-blob-common 0.1.28 ProgramBlob::from_pvm, on the 0.36 ProgramParts.
    let mut ro_data = parts.ro_data.to_vec();
    ro_data.resize(parts.ro_data_size as usize, 0);
    let padding = (parts.rw_data_size as usize).next_multiple_of(4096) - parts.rw_data.len().next_multiple_of(4096);
    let rw_data_padding_pages: u16 = (padding / 4096).try_into().unwrap_or_else(|_| die("RW data too big"));
    let metadata = ConventionalMetadata::Info(info.clone()).encode();
    let blob = ProgramBlob {
        metadata: metadata.into(),
        ro_data: ro_data.into(),
        rw_data: parts.rw_data.to_vec().into(),
        code_blob: parts.code_and_jump_table.to_vec().into(),
        rw_data_padding_pages,
        stack_size: parts.stack_size,
    }
    .to_vec()
    .unwrap_or_else(|e| die(e));

    std::fs::create_dir_all(&out_dir).unwrap_or_else(|e| die(e));
    let out = out_dir.join(format!("{}.jam", info.name));
    std::fs::write(&out, &blob).unwrap_or_else(|e| die(e));
    let hash = blake2b_simd::Params::new().hash_length(32).hash(&blob);
    println!("{} ({} bytes) code_hash 0x{}", out.display(), blob.len(), hash.to_hex());
}
