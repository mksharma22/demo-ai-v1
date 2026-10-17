# Portable way to get libpostal: build it inside a Debian image.
# Image is large (~3 GB) because libpostal's model data is ~2 GB.
# NOTE: this Dockerfile was not built in the authoring environment (no Docker
# there); its steps mirror scripts/install_libpostal.sh, which was run.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl git autoconf automake libtool pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG LIBPOSTAL_DATA=/opt/libpostal_data
RUN git clone --depth 1 https://github.com/openvenues/libpostal.git /opt/src/libpostal \
    && cd /opt/src/libpostal && ./bootstrap.sh \
    && ./configure --datadir=${LIBPOSTAL_DATA} $( [ "$(uname -m)" = "aarch64" ] && echo --disable-sse2 ) \
    && make -j"$(nproc)" && make install && ldconfig \
    && rm -rf /opt/src/libpostal

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --use-pep517 -r requirements.txt
COPY . .

RUN useradd --create-home appuser && mkdir -p /app/.data && chown -R appuser /app/.data
USER appuser
ENV APP_DATA_DIR=/app/.data/jobs
EXPOSE 8501
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
