#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# dynamic list
IMAGE_LIST=()
IMAGE_LIST+=($(grep "self._metacontroller_image =" src/charm.py | awk '{print $3}' | sort --unique | sed s/,//g | sed s/\"//g))
printf "%s\n" "${IMAGE_LIST[@]}"

