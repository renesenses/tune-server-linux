class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.14"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.13-macos-aarch64.tar.gz"
      sha256 "36089775621bac1cbdf162ce81f843c326ce3e85b6ab9e031bdb4895b1947c34"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.13-macos-x86_64.tar.gz"
      sha256 "7b0916830561b8fb2a31961811dce9bd93518b92482999a69aab33c1a83c9f41"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.13-linux-aarch64.tar.gz"
      sha256 "f2dd1509add293bfaaf0ab26ec4a71a0687021e614591b9c5ccbf2d742ae2f04"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.14/tune-server-v0.8.13-linux-x86_64.tar.gz"
      sha256 "ad8ee6d7c124c4a53effc09c78432f7c0c4960e032c4e9f222c05af1a9d077bd"
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
