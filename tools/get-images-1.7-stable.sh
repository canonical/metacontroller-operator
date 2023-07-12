#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# static list
STATIC_IMAGE_LIST=(
)
# dynamic list
git checkout origin/track/2.0
IMAGE_LIST=()
IMAGE_LIST+=($(grep "self._metacontroller_image =" src/charm.py | awk '{print $3}' | sort --unique | sed s/,//g | sed s/\"//g))

printf "%s\n" "${STATIC_IMAGE_LIST[@]}"
printf "%s\n" "${IMAGE_LIST[@]}"
