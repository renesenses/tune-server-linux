class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.15"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.15/tune-server-v0.8.15-macos-aarch64.tar.gz"
      sha256 "867cc4ab5e1f62a1e63542b626c2cf0512af5d2abb07b5967d0ccdd6d069a07a"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.15/tune-server-v0.8.15-macos-x86_64.tar.gz"
      sha256 "9c0b7cba5c3acdb18d43c57f22c36d5a3c32454796edd404b1f8a129423d5cf2"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.15/tune-server-v0.8.15-linux-aarch64.tar.gz"
      sha256 "ca181cd61dca233574303d4aa10a03ed029fed078bdbe60d2fa0dee7b8bbeedc"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.15/tune-server-v0.8.15-linux-x86_64.tar.gz"
      sha256 "51ea9aa521d7165d08a041b867b08125f2ac7b05d4f6fc0d7d95b083cb5c71e1"
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
      Tune Server v0.8.15 (Rust) installed!

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
