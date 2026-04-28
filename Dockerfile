FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# System tools needed by tce.sh and the CPAchecker build in make lib/cpachecker.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ant \
    binutils \
    build-essential \
    ca-certificates \
    clang \
    gcc \
    git \
    make \
    openjdk-17-jdk-headless \
    unzip \
    zsh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
    && make lib/pip

# Build CPAchecker during image construction so check.py can run immediately.
RUN make lib/cpachecker

CMD ["/bin/zsh"]
