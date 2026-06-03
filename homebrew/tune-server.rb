class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.29"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.29-macos-aarch64.tar.gz"
      sha256 "1f0d22adbe42d01e7f0cc6a66b972fbb21446f6125f119582706ca7984bfbd7f"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.29-macos-x86_64.tar.gz"
      sha256 "aa3e28184dc3047c994f71798e0066645aceb7945224c8478c7e8fde3be3a4c6"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.29-linux-aarch64.tar.gz"
      sha256 "b7fc0081ac4042c4e2f2cff6404bc8bb2477c4134a62fd10bab3f4e63d007678"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.29/tune-server-v0.8.29-linux-x86_64.tar.gz"
      sha256 "1bedd073dcc5d04e7147fe027767bab076d91819b9f8675f2bd90a73c79ce62f"
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
