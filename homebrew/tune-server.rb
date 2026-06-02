class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.24"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.24/tune-server-v0.8.23-macos-aarch64.tar.gz"
      sha256 "bce2059e5d4a83f8bab4696cdbc4856573f19b63a37d947e25fa4cf9198095ac"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.24/tune-server-v0.8.23-macos-x86_64.tar.gz"
      sha256 "7cf809e9d305772c1acddb92b74cfb61cc586aae13e72fcbf333ea58552c6bf7"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.24/tune-server-v0.8.23-linux-aarch64.tar.gz"
      sha256 "26853fed87a80b317213075a383faf6adda0f88525cd4ae7572bcd4e6e86f930"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.24/tune-server-v0.8.23-linux-x86_64.tar.gz"
      sha256 "dd4fb5cc374b1583b7ebce0a052f503a7b65f78463e4ff20ef778602c48d28d2"
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
      Tune Server v0.8.24 (Rust) installed!

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
