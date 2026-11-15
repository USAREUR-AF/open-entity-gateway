# Dstl's Apex SAPIENT middleware (Apache-2.0), built from source at a pinned
# commit, for the SAPIENT end-to-end test. Not part of any release.
FROM python:3.11-slim
ARG APEX_COMMIT=0c8591ae97f2dc336eff299845880d5c73ee75b7
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/dstl/Apex-SAPIENT-Middleware.git /apex \
 && git -C /apex checkout --quiet "$APEX_COMMIT"
WORKDIR /apex
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
 && pip install --no-cache-dir --no-deps .
COPY apex_config.json /apex/apex_config.json
ENV PYTHONPATH=/apex
CMD ["python", "sapient_apex_server/apex.py"]
