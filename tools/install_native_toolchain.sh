#!/usr/bin/env bash
# MettaGrid 0.26.15 is distributed as source, including C++ and Nim extensions.
set -euo pipefail
[[ "$(uname -m)" == x86_64 ]]
apt-get update
apt-get install -y --no-install-recommends build-essential ca-certificates-java curl git libssl-dev libopenal-dev
install -d /usr/local/libexec
curl -fsSL https://releases.bazel.build/9.0.0/release/bazel-9.0.0-linux-x86_64 -o /usr/local/libexec/bazel
printf '%s  %s\n' c44a93f25398c68f904fa1d19b61d321de6c0d2f09dca375d7bc0dc9b9428403 /usr/local/libexec/bazel | sha256sum -c -
chmod +x /usr/local/libexec/bazel
# The pinned package passes --jobs=auto explicitly; bound its compiler memory.
cat > /usr/local/bin/bazel <<'BAZEL'
#!/usr/bin/env bash
exec /usr/local/libexec/bazel --host_jvm_args=-Xmx512m "${@/--jobs=auto/--jobs=1}"
BAZEL
chmod +x /usr/local/bin/bazel
curl -fsSL https://github.com/treeform/nimby/releases/download/0.1.27/nimby-Linux-X64 -o /usr/local/bin/nimby
printf '%s  %s\n' 3b3084394bd26b09f84a3f82389f075221c8784893238390939d71dd66ac9e8b /usr/local/bin/nimby | sha256sum -c -
chmod +x /usr/local/bin/nimby
nimby use 2.2.4
cp -aL "$HOME/.nimby/nim" /opt/nim-2.2.4
printf '\n--parallelBuild:1\n' >> /opt/nim-2.2.4/config/nim.cfg
ln -s /opt/nim-2.2.4/bin/nim /usr/local/bin/nim
