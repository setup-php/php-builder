#!/usr/bin/env bash

container_os_json_array=()
runner_os_json_array=()
test_container_os_json_array=()
test_runner_os_json_array=()
container_only='false'

# Store input container os versions in container_os_array,
# Store input runner os versions in runner_os_array,
# PHP SAPIs in sapi_array,
# and PHP versions to be built in php_array.
IFS=' ' read -r -a container_os_array <<<"${CONTAINER_OS_LIST:?}"
IFS=' ' read -r -a runner_os_array <<<"${RUNNER_OS_LIST:?}"
IFS=' ' read -r -a build_array <<<"${BUILD_LIST:?}"
IFS=' ' read -r -a php_array <<<"$(bash scripts/check-php-version.sh "${PHP_LIST:?}" "${COMMIT:-'--build-new'}" "${PHP_SOURCE:-'--web-php'}")"

get_container_base() {
  [[ $1 = *arm64v8* ]] && echo "${CONTAINER_BASE_ARM:?}" || echo "${CONTAINER_BASE:?}"
}

get_dist_version() {
  [[ $1 = *arm64v8* ]] && echo "${os##*:}-arm" || echo "${os##*:}"
}

get_dist() {
  echo "${os%:*}" | cut -d'/' -f 2
}

# Build a matrix array with container, distribution, distribution version and php-version and OS
for os in "${container_os_array[@]}"; do
  for php in "${php_array[@]}"; do
    for build in "${build_array[@]}"; do
      dist="$(get_dist "$os")"
      dist_version="$(get_dist_version "$os")"
      os_base="$(get_container_base "$os")"
      container_os_json_array+=("{\"container\": \"$os\", \"container-base\": \"$os_base\", \"php-version\": \"$php\", \"dist\": \"$dist\", \"dist-version\": \"$dist_version\", \"build\": \"$build\"}")
    done
  done
done

# Build a matrix array with runner os, php-version and the build.
for os in "${runner_os_array[@]}"; do
  for php in "${php_array[@]}"; do
    for build in "${build_array[@]}"; do
      runner_os_json_array+=("{\"os\": \"$os\", \"php-version\": \"$php\", \"build\": \"$build\"}")
    done
  done
done

# Build a matrix array with runner os, php-version, build and the debug.
for os in "${container_os_array[@]}"; do
  for php in "${php_array[@]}"; do
    for build in "${build_array[@]}"; do
      for debug in debug release; do
        dist="$(get_dist "$os")"
        dist_version="$(get_dist_version "$os")"
        os_base="$(get_container_base "$os")"
        test_container_os_json_array+=("{\"container\": \"$os\", \"container-base\": \"$os_base\", \"php-version\": \"$php\", \"dist\": \"$dist\", \"dist-version\": \"$dist_version\", \"build\": \"$build\", \"debug\": \"$debug\"}")
      done
    done
  done
done

# Build a matrix array with runner os, php-version, build and the debug.
for os in "${runner_os_array[@]}"; do
  for php in "${php_array[@]}"; do
    for build in "${build_array[@]}"; do
      for debug in debug release; do
        test_runner_os_json_array+=("{\"os\": \"$os\", \"php-version\": \"$php\", \"build\": \"$build\", \"debug\": \"$debug\"}")
      done
    done
  done
done

for runner_os in "${runner_os_array[@]}"; do
  container_runner_os="${runner_os//-/:}"
  if [[ $CONTAINER_OS_LIST != *$container_runner_os* ]]; then
    container_only='true'
    break
  fi
done

# Output the matrices.
(
  # shellcheck disable=SC2001
  echo "container_os_matrix={\"include\":[$(echo "${container_os_json_array[@]}" | sed -e 's|} {|}, {|g')]}";
  # shellcheck disable=SC2001
  echo "runner_os_matrix={\"include\":[$(echo "${runner_os_json_array[@]}" | sed -e 's|} {|}, {|g')]}";
  # shellcheck disable=SC2001
  echo "test_container_os_matrix={\"include\":[$(echo "${test_container_os_json_array[@]}" | sed -e 's|} {|}, {|g')]}";
  # shellcheck disable=SC2001
  echo "test_runner_os_matrix={\"include\":[$(echo "${test_runner_os_json_array[@]}" | sed -e 's|} {|}, {|g')]}";
  echo "container_only=$container_only"
) >> "$GITHUB_OUTPUT"
