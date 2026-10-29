# AgentMeter cloud data preparation (Google Cloud Run). Built from a staging folder that
# scripts/deploy_cloudprep.sh assembles: this file, app.py, requirements.txt, the browser's
# preparation bundle (web/prep/agentmeter-prep.zip: the same code the GPU server runs), its two
# pinned wheels and agentmeter/server/urlfetch.py (SSRF-safe link imports).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt ./
COPY wheels/ ./wheels/
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --no-deps wheels/*.whl \
 && rm -rf wheels
COPY agentmeter-prep.zip urlfetch.py app.py ./
RUN python -c "import zipfile; zipfile.ZipFile('agentmeter-prep.zip').extractall('.')" \
 && mv urlfetch.py agentmeter/server/urlfetch.py && rm agentmeter-prep.zip \
 && python -c "import app"
USER nobody
CMD ["python", "app.py"]
