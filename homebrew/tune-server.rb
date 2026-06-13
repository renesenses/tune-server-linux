class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.99"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.99/tune-server-v0.8.90-macos-aarch64.tar.gz"
      sha256 "eb88be3b6331c5180ee760d4465f41a9844b637db762b75eeb39649d6294b507"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.99/tune-server-v0.8.90-macos-x86_64.tar.gz"
      sha256 "077cf928e5a11f552496cad9ce81576981fc0c95282afcf0ea7ca9f3f7ac4fbf"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.99/tune-server-v0.8.90-linux-aarch64.tar.gz"
      sha256 "8b4b29a65fa4110e517aea2fbcfdf667a7550488d6cb2deadcddea7ae8732f50"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.99/tune-server-v0.8.90-linux-x86_64.tar.gz"
      sha256 "840244c9d67cbe9e85851604454ee1051b3c380c688b5f8c8be56d5bfd6ef8e0"
    end
  end

  def install
    bin.install "tune-server"
    pkgshare.install "web"

    (bin/"tune-server-launcher").write <<~EOS
      #!/bin/bash
      export PATH="#{Formula["ffmpeg"].opt_bin}:$PATH"
      export TUNE_PORT="${TUNE_PORT:-8888}"
      export TUNE_WEB_DIR="#{pkgshare}/web"
      exec "#{bin}/tune-server" "$@"
    EOS
    chmod 0755, bin/"tune-server-launcher"
  end

  def post_install
    (var/"tune-server").mkpath
    (var/"tune-server/artwork_cache").mkpath
  end

  def caveats
    <<~EOS
      Tune Server v0.8.99 (Rust) installed!

      Start: tune-server-launcher
      Web UI: http://localhost:8888

      Background service: brew services start tune-server

      Legacy Python version: brew install renesenses/tap/tune-server-python
    EOS
  end

  service do
    run [opt_bin/"tune-server-launcher"]
    working_dir var/"tune-server"
    keep_alive true
    log_path var/"log/tune-server.log"
    error_log_path var/"log/tune-server.log"
    environment_variables PATH: std_service_path_env
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tune-server --version 2>&1", 0)
  end
end
