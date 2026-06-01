class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.20"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.20/tune-server-v0.8.19-macos-aarch64.tar.gz"
      sha256 "1e0b04e72b6d4e03bb72bf3a2bebe17a70647c8c6d1a373732bd716799290c4f"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.20/tune-server-v0.8.19-macos-x86_64.tar.gz"
      sha256 "ceada59e902110b40a48e718d062852fc1e5c8f32da250444adc2e78b0f2f454"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.20/tune-server-v0.8.19-linux-aarch64.tar.gz"
      sha256 "9d41cab905d4d080f6f33ea3a7b8384a9389841af5e41fac5b0196e6f1646505"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.20/tune-server-v0.8.19-linux-x86_64.tar.gz"
      sha256 "4f9658c579183dc9a3a6115ed6bba61fbd96ac6fc69afaf25ec1678cabdddc08"
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
      Tune Server v0.8.20 (Rust) installed!

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
