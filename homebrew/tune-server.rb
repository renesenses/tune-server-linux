class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.45"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.45/tune-server-v0.8.42-macos-aarch64.tar.gz"
      sha256 "640aa6812c0ccaf4b98d2d77f8a46319c6767c189fe1294c3fe7b1feaaf3e764"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.45/tune-server-v0.8.42-macos-x86_64.tar.gz"
      sha256 "a6ab1fe24425a279f798e9ca9001d5c0e952f3292d2e7dcc89ac578a3cfe56b8"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.45/tune-server-v0.8.42-linux-aarch64.tar.gz"
      sha256 "e9f1a43683e6ca1afd1bf1447f19240deadc3c235f9c094846ba92e1a7b94db1"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.45/tune-server-v0.8.42-linux-x86_64.tar.gz"
      sha256 "437ae85c88976104faac709743da1127f8df8cb11733204e010f0cc89e595288"
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
      Tune Server v0.8.45 (Rust) installed!

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
