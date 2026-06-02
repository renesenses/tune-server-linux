class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.21"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.21-macos-aarch64.tar.gz"
      sha256 "5c969b10a15f302e8bd42dd8d8fd1ea23b528b192051d9b11c408bf7e3d7e100"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.21-macos-x86_64.tar.gz"
      sha256 "987a87b85f00abe81801447cd2a94ccd06a39035e99b8a0e71504140d154ed90"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.21-linux-aarch64.tar.gz"
      sha256 "850acebe65a68f97f1a57c3e6b115ecd537d72f788ed863a9afb9d6e434a4744"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.21/tune-server-v0.8.21-linux-x86_64.tar.gz"
      sha256 "5fc1878b72d44d688742f1c4aae34b9e2dd1cee33972d36b2572107eeaa18d2f"
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
