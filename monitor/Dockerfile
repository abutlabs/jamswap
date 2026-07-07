# Mixed-net metrics exporter: stdlib-only Python, reads the Docker log API
# (read-only socket mount) + pj0's JSON-RPC. See exporter.py.
FROM python:3.12-alpine
COPY monitor/exporter.py /exporter.py
EXPOSE 9105
CMD ["python3", "-u", "/exporter.py"]
