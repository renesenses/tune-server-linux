class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.26"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.25-macos-aarch64.tar.gz"
      sha256 "81bc954d1bafda144d989596ceff6fe37b509dc7a1971de48cd90248444d7785"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.25-macos-x86_64.tar.gz"
      sha256 "29be2be3aac1734ca6293b0f1379aee25ed94fe2ed9eed5f9b3d184135ebe83c"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.25-linux-aarch64.tar.gz"
      sha256 "628e013afc6dad27b8fd65dcd74ca4d73118b8b066ec259f67ee1a7c34745447"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.26/tune-server-v0.8.25-linux-x86_64.tar.gz"
      sha256 "a82c301062119223a78b233caf012a250e0085de63d87c3bfb0e8f17de642c83"
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
