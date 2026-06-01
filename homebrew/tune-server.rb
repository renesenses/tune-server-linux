class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.14"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.14-macos-aarch64.tar.gz"
      sha256 "f207884bfe318d651a0dc5873df07b86e587b229b6cb5824a8d65a430d623dfb"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.14-macos-x86_64.tar.gz"
      sha256 "018b20e688f74eef9fd037f202e8bfc8cab3956cae6937060fb8de8541d83e17"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.14-linux-aarch64.tar.gz"
      sha256 "07056cb2109cbd2a437c175842db980f41942bc0a7fc939a50813507fb34aaf0"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.14-linux-x86_64.tar.gz"
      sha256 "fa6d88a4ce72a5864ae5aa2a9b4f99b1adb78968ed8199edadcd4965f4cf506d"
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
      Tune Server v0.8.14 (Rust) installed!

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
