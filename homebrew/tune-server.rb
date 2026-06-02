class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.21"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.20-macos-aarch64.tar.gz"
      sha256 "27f80c09ccf49ec8aab3287f4a58d8ca6de524c56930addf14b52913fbab3cfc"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.20-macos-x86_64.tar.gz"
      sha256 "3ab806cefcc9dabd3fa28a98490d93e87d0247e22ae667b31d794f17948e0804"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.20-linux-aarch64.tar.gz"
      sha256 "f6652f2a5843093b74d8cac6eb9ca07319eb9df482857cc75a5f32d8d28a39ac"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.20-linux-x86_64.tar.gz"
      sha256 "3d89677af1de413e30a5a50bde717e6aea54447fb4fefed50c2bf51de3edd7a8"
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
      Tune Server v0.8.21 (Rust) installed!

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
