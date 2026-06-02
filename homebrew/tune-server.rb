class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.25"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.25/tune-server-v0.8.24-macos-aarch64.tar.gz"
      sha256 "07e710b76318fbfbcd28dd59f6ce9fc0dc53c6a39d5649168356d9e1f3515f10"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.25/tune-server-v0.8.24-macos-x86_64.tar.gz"
      sha256 "23ba418ed589c385726723215123fa4593ee87159264dcee2b2f6fac479ffa4c"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.25/tune-server-v0.8.24-linux-aarch64.tar.gz"
      sha256 "ce36816c9b48d5d324d835bdf9eb14589fc4e926a6097801a39dae1762ce24c5"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.25/tune-server-v0.8.24-linux-x86_64.tar.gz"
      sha256 "9064d7fddca59679676648e8cc823c3399780874fb96db264d1951a02a90aad2"
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
      Tune Server v0.8.25 (Rust) installed!

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
