#!/bin/sh
set -e

if [ ! -f /data/map.osm.pbf ]; then
    echo "ERROR: no OSM file mounted at /data/map.osm.pbf" >&2
    exit 1
fi

# `serve` imports the graph into /graph-cache first if it is empty,
# so the first start can take many minutes depending on the extract size.
echo "Starting OpenRailRouting (first start imports the graph, this can take a while)..."
exec java ${JAVA_OPTS:--Xmx4g -Xms1g} -jar /app/railway_routing.jar serve /app/config.yml
