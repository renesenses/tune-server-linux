class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.34"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.34/tune-server-v0.8.34-macos-aarch64.tar.gz"
      sha256 "aa4bae2afdb63df5e2a4980c1bb100cd3e5f79fdde3b8fffc0e436c47b305d9f"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.34/tune-server-v0.8.34-macos-x86_64.tar.gz"
      sha256 "0938906c1a00b6aa946cd3fdaaad76031ad7353310ae85c04cdb407f6f31ee26"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.34/tune-server-v0.8.34-linux-aarch64.tar.gz"
      sha256 "60ab70659cb7dc1e4a0d8bb2a363dc279d65e76b9e6f8ae54bccd9042c9dc145"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.34/tune-server-v0.8.34-linux-x86_64.tar.gz"
      sha256 "a6ca0f830c1552f27cac718bcd0e907c071759d6bcfa01153ea95c416be57fc1"
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
      Tune Server v0.8.34 (Rust) installed!

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
