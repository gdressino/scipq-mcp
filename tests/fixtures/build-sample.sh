#!/usr/bin/env bash
# Regenerates the SCIP shards of tests/fixtures/sample with javac + the scip-javac plugin,
# using the same layout as the Gradle init template:
#   build/scip-targetroot/<module>/<task>/META-INF/scip/<source path relative to the root>.scip
#
# Needs a JDK 17+ on PATH. Jars come from the Gradle cache when present, otherwise from Maven
# Central (cached under tests/fixtures/.jars, which is git-ignored). Only the shards are kept:
# class files are written to a temporary directory.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/sample"
JARS="$HERE/.jars"
SCIP_VERSION="${SCIP_VERSION:-0.14.2}"

ARTIFACTS=(
  "org.scip-code:scip-javac:$SCIP_VERSION"
  "org.scip-code:scip-shared:$SCIP_VERSION"
  "org.scip-code:scip-java-bindings:0.10.0"
  "com.google.protobuf:protobuf-java:4.34.2"
  "com.google.guava:guava:33.7.2-jre"
  "com.google.guava:failureaccess:1.0.3"
  "org.projectlombok:lombok:1.18.42"
)

jar_path() {
  local group="${1%%:*}" rest="${1#*:}"
  local name="${rest%%:*}" version="${rest#*:}"
  local file="$name-$version.jar"
  local cached
  cached="$(find "$HOME/.gradle/caches/modules-2/files-2.1/$group/$name/$version" \
    -name "$file" 2>/dev/null | head -n1 || true)"
  if [[ -n "$cached" ]]; then
    echo "$cached"
    return
  fi
  mkdir -p "$JARS"
  if [[ ! -f "$JARS/$file" ]]; then
    curl -fsSL -o "$JARS/$file.tmp" \
      "https://repo1.maven.org/maven2/${group//.//}/$name/$version/$file"
    mv "$JARS/$file.tmp" "$JARS/$file"
  fi
  echo "$JARS/$file"
}

PROCESSORPATH=""
LOMBOK=""
for a in "${ARTIFACTS[@]}"; do
  p="$(jar_path "$a")"
  PROCESSORPATH="${PROCESSORPATH:+$PROCESSORPATH:}$p"
  [[ "$a" == org.projectlombok:* ]] && LOMBOK="$p"
done

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"; rm -f "$ROOT/core/src/main/java/com/example/model/Gone.java"' EXIT

rm -rf "$ROOT/build/scip-targetroot"

# compile_in <project root> <module> <task> <classes out> <classpath> <source dir>
compile_in() {
  local root="$1" module="$2" task="$3" classes="$4" classpath="$5" srcdir="$6"
  local targetroot="$root/build/scip-targetroot/$module/$task"
  mkdir -p "$classes"
  # shellcheck disable=SC2046
  javac -encoding UTF-8 -d "$classes" -cp "$classpath" -processorpath "$PROCESSORPATH" \
    "-Xplugin:scip -sourceroot:$root -targetroot:$targetroot" \
    $(find "$root/$srcdir" -name '*.java' | sort)
}

compile() { compile_in "$ROOT" "$@"; }

# Gone.java is compiled and then removed, leaving an orphan shard (the source no longer exists).
GONE="$ROOT/core/src/main/java/com/example/model/Gone.java"
cp "$HERE/orphan/Gone.java" "$GONE"
compile core compileJava "$OUT/core" "$LOMBOK" core/src/main/java
rm -f "$GONE"
compile app compileJava "$OUT/app" "$LOMBOK:$OUT/core" app/src/main/java
compile app compileTestJava "$OUT/app-test" "$LOMBOK:$OUT/core:$OUT/app" app/src/test/java

# A second, small project: Cucumber glue and features, with stand-ins for Cucumber's
# annotations (no Cucumber jar needed).
CROOT="$HERE/cucumber"
rm -rf "$CROOT/build/scip-targetroot"
compile_in "$CROOT" shop compileJava "$OUT/shop" "" shop/src/main/java
compile_in "$CROOT" shop compileTestJava "$OUT/shop-test" "$OUT/shop" shop/src/test/java

# A third one for refactoring: persistence (stand-in JDO annotations), Lombok, shadowing,
# overloads, static imports and javadoc links.
RROOT="$HERE/refactor"
rm -rf "$RROOT/build/scip-targetroot"
compile_in "$RROOT" shop compileJava "$OUT/refactor" "$LOMBOK" shop/src/main/java

find "$ROOT/build/scip-targetroot" -type f -name '*.scip' | sort | sed "s|$ROOT/||"
find "$CROOT/build/scip-targetroot" -type f -name '*.scip' | sort | sed "s|$HERE/||"
find "$RROOT/build/scip-targetroot" -type f -name '*.scip' | sort | sed "s|$HERE/||"
