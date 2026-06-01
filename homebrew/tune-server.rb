class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.19"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.19/tune-server-v0.8.18-macos-aarch64.tar.gz"
      sha256 "c64de0e3e174ee17024892b7376d823313e094b46304147a9fd48b980d20ce70"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.19/tune-server-v0.8.18-macos-x86_64.tar.gz"
      sha256 "f246e31e04e41ec9e0d7ffc4dca07f023dd790d679904663cc12ec3b0a99e883"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.19/tune-server-v0.8.18-linux-aarch64.tar.gz"
      sha256 "5698cd2b69fe832b5372e9dcae2dab5b6558251a57d5aad87426cd2b2c0e969d"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.19/tune-server-v0.8.18-linux-x86_64.tar.gz"
      sha256 "4770a0d47264dfe485c548adbb174e9190b0deceab3b45fc484dfbee29a9e775"
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
      Tune Server v0.8.19 (Rust) installed!

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
