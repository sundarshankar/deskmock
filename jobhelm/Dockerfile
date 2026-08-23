# JobHelm Mission Control — portable dashboard + LLM/prep features.
# Pure Python stdlib; no pip install needed.
FROM python:3.12-slim

WORKDIR /app
COPY mission-control.py ./
COPY sample-data ./sample-data

# By default it serves the bundled sample data. Mount your own career-ops /
# DeskMock checkouts and point these at them (see docker-compose.yml).
ENV JOBHELM_CAREEROPS=/app/sample-data \
    JOBHELM_MOCK=/app/sample-data/mock \
    JOBHELM_HOST=0.0.0.0 \
    JOBHELM_PORT=8899

EXPOSE 8899
CMD ["python3", "mission-control.py"]
