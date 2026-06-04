class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.42"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.42/tune-server-v0.8.41-macos-aarch64.tar.gz"
      sha256 "02170d73a2fef651fa1c3935820c154b74ca716707ad6331f2c3933d76f7ad8e"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.42/tune-server-v0.8.41-macos-aarch64.tar.gz"
      sha256 "02170d73a2fef651fa1c3935820c154b74ca716707ad6331f2c3933d76f7ad8e"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.42/tune-server-v0.8.41-linux-aarch64.tar.gz"
      sha256 "7115a5361216df709df31a95860e3e5fc436d255ff44c3552042b5ffebbe2f29"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.42/tune-server-v0.8.41-linux-x86_64.tar.gz"
      sha256 "dcb9528235c635507a0caee02eadbf61b9b29c4e9680f1e0bdc855836041eefc"
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
      Tune Server v0.8.42 (Rust) installed!

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
