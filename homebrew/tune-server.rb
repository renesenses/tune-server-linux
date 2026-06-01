class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.16"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.16/tune-server-v0.8.16-macos-aarch64.tar.gz"
      sha256 "088743613b3ccb9392a4fff0f91fa7a6d9bb54ac6ccd4d5633b0b112c56e18ab"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.16/tune-server-v0.8.16-macos-x86_64.tar.gz"
      sha256 "f2c463fcb8f95ce34b2e5aabd1b16adaae706523ee54c05b1cc54ee3021cf456"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.16/tune-server-v0.8.16-linux-aarch64.tar.gz"
      sha256 "1962baa6393f56607a38a9890f6390d9fad24d44347ef83bc66cf1b3c19a2c61"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.16/tune-server-v0.8.16-linux-x86_64.tar.gz"
      sha256 "d0a6315796bbf1fbc885b95df4f26cf12084951fac474d0148f3880647183e10"
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
      Tune Server v0.8.16 (Rust) installed!

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
