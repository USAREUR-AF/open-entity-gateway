# Runs the end-to-end tests next to the servers, on their compose network.
FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY requirements/ requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/runtime.txt -r requirements/dev.txt -r requirements/tools.txt
COPY . .
RUN pip install --no-cache-dir --no-deps -e . \
 && python tools/fetch-cot-xsd.py && python tools/fetch-sapient-proto.py
ENTRYPOINT ["python", "-m", "pytest", "-v", "-rA", "tests/e2e"]
