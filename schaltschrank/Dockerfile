# A fixed, multi-arch base with Python (newer Supervisors no longer pass BUILD_FROM; the platform comes from
# the build's --platform).
FROM python:3.12-alpine

# The packages for the Excel import/export.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The app only; the database lives in /data and comes in through "Datenbank importieren".
COPY app.py connections.py ets_import_common.py home_assistant.py run.sh /app/
COPY web /app/web
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
