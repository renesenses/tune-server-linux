class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.32"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.32/tune-server-v0.8.32-macos-aarch64.tar.gz"
      sha256 "bf8d4e52ffd94e7036c702caca044f777e43a288e00b334c6e9836afb412b5da"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.32/tune-server-v0.8.32-macos-x86_64.tar.gz"
      sha256 "c00b5f1e15af92f1bd078dd8c4201c29ffa0d0797ddbf6283ffa45158d62efd9"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.32/tune-server-v0.8.32-linux-aarch64.tar.gz"
      sha256 "aa3b48bb3771091d2d030790666a014a6d4d4ef82c2ecadc8d2d1cd3a7db9256"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.32/tune-server-v0.8.32-linux-x86_64.tar.gz"
      sha256 "720a26f440425241152a12682c20af2a87e67d30262d52921426f6ad450552b7"
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
      Tune Server v0.8.32 (Rust) installed!

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
