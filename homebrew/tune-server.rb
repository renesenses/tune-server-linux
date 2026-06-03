class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.29"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.28-macos-aarch64.tar.gz"
      sha256 "47e97473a576afa36db11552078b03b5d8f1b6298d11c3df7484175a08d7230a"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.28-macos-x86_64.tar.gz"
      sha256 "8401f0c7157f28f736bf60271309261df3dbbb4436f98c0715c2f3f844ee377d"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.28-linux-aarch64.tar.gz"
      sha256 "fd472f108394cf6aaa46759782a368bc462f161e813d13bf6b568c55790de6dd"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.28-linux-x86_64.tar.gz"
      sha256 "3db2417aa2e173c1724a44d80326760ad106b3c570adae19d2d7ec8761a25d74"
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
      Tune Server v0.8.29 (Rust) installed!

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
