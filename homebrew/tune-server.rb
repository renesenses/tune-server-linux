class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.30"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.30/tune-server-v0.8.30-macos-aarch64.tar.gz"
      sha256 "02a9c9b1f8c8f6101847fbb673c957f22707a2e60f738104dffff23da660e127"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.30/tune-server-v0.8.30-macos-x86_64.tar.gz"
      sha256 "5bad91ba517dcef7abb3d919c18b48291072d38a649cf77963b73d4589bd002c"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.30/tune-server-v0.8.30-linux-aarch64.tar.gz"
      sha256 "7be3fef0380136836cec87e513fb313d9daebcd766887916022411318966d1b5"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.30/tune-server-v0.8.30-linux-x86_64.tar.gz"
      sha256 "32281e8e5479250af9781ea1db0abfd62dfee4a68c6732f7d1f073f686decef2"
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
      Tune Server v0.8.30 (Rust) installed!

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
