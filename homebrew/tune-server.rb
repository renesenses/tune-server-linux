class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.26"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.26-macos-aarch64.tar.gz"
      sha256 "0a0c1e3e3901428bac80a3fba176f30856b0b5d5f502b7c853ea99d4f54720c6"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.26-macos-x86_64.tar.gz"
      sha256 "0828273864441c99233247dcb370c4f4dbc41b3ef4a776f2451726ea79591a72"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.26-linux-aarch64.tar.gz"
      sha256 "0247e70d598fe11dd7c6736d3c4df7031e7edcc9bbfe6566f9d975603cb9d447"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.26-linux-x86_64.tar.gz"
      sha256 "3319cf63cc33f9533b0113446a689dfe5c26f88b2bfe122fd372f9127ee64436"
    end
  end

  depends_on "ffmpeg"

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
      Tune Server v0.8.26 (Rust) installed!

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
