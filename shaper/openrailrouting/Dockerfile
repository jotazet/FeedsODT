# OpenRailRouting (https://github.com/geofabrik/OpenRailRouting) server image.
# GraphHopper fork by Geofabrik that supports routing on railway tracks
# (train / tram / light_rail), used for GTFS route_type 0 (tram) and 2 (rail).

FROM maven:3.9-eclipse-temurin-17 AS build

ARG ORR_VERSION=v1.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --branch ${ORR_VERSION} --depth 1 --recurse-submodules --shallow-submodules \
    https://github.com/geofabrik/OpenRailRouting.git /src

WORKDIR /src
RUN mvn --batch-mode -DskipTests clean package

# Prefer the shaded jar-with-dependencies if the build produced one.
RUN mkdir /out \
    && { cp /src/target/railway_routing-*-jar-with-dependencies.jar /out/railway_routing.jar 2>/dev/null \
         || cp "$(ls /src/target/railway_routing-*.jar | grep -v sources | head -n 1)" /out/railway_routing.jar; }

FROM eclipse-temurin:17-jre

WORKDIR /app
COPY --from=build /out/railway_routing.jar /app/railway_routing.jar
COPY config.yml /app/config.yml
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8989
ENTRYPOINT ["/entrypoint.sh"]
