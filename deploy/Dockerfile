ARG ARENA_BASE_IMAGE=ubuntu:24.04
FROM ${ARENA_BASE_IMAGE} AS build

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build default-libmysqlclient-dev protobuf-compiler libprotobuf-dev ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DARENA_ENABLE_SANITIZERS=OFF \
    && cmake --build build --target arena_server -j2

FROM ${ARENA_BASE_IMAGE} AS runtime
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates libmysqlclient21 libprotobuf32t64 netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /src/build/arena_server /app/arena_server
COPY --from=build /src/server/config /app/config
EXPOSE 9000
ENTRYPOINT ["/app/arena_server"]
