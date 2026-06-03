class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.31"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.31/tune-server-v0.8.31-macos-aarch64.tar.gz"
      sha256 "c9860ad3c3f22743b8fed1869eb6fbca874a8c1757dab9db06e24d85f95b2d3a"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.31/tune-server-v0.8.31-macos-x86_64.tar.gz"
      sha256 "7ecb73145093daa5b6407a326f07e948e16473e5d50a752e0b313cf7378990e4"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.31/tune-server-v0.8.31-linux-aarch64.tar.gz"
      sha256 "00a0801b66484a50973f15d13d97f3bee64afdfc4ff22007e27da62184429b67"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.31/tune-server-v0.8.31-linux-x86_64.tar.gz"
      sha256 "6f7ef5b2cbfa0eff9df4651c52333f155671fd35d904621dec52ea4c97450140"
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
      Tune Server v0.8.31 (Rust) installed!

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
