class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.18"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.18/tune-server-v0.8.17-macos-aarch64.tar.gz"
      sha256 "1fa8831c79c90c66657c5c53a56ebdb6989c11e44fc525a1dc25e85e159aa7f4"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.18/tune-server-v0.8.17-macos-x86_64.tar.gz"
      sha256 "0e6d1a4ca1969a55e3f1bbd2d0ac175d315512ded6cdeb5be2cd3a011885195d"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.18/tune-server-v0.8.17-linux-aarch64.tar.gz"
      sha256 "171fa45fb552631e0eee3b38412f8a092b88d5f1f12ba6e19a1d678a7b1db008"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.18/tune-server-v0.8.17-linux-x86_64.tar.gz"
      sha256 "3298f0e69eabe29aa2f569a50e41b6dae2016f6deb1e37b1c9fcc47c97f72ebf"
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
      Tune Server v0.8.18 (Rust) installed!

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
