class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.33"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.33/tune-server-v0.8.33-macos-aarch64.tar.gz"
      sha256 "2158a1b3e6ef6f3ea6ed598ed6a53f7def20f83813d3eca02fcebd3973d4314c"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.33/tune-server-v0.8.33-macos-x86_64.tar.gz"
      sha256 "936e18ee600e5a93c7ec654c9789803daa63c7b9e2eb14718bc4f37757dbbfbb"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.33/tune-server-v0.8.33-linux-aarch64.tar.gz"
      sha256 "5e7b76ae9a82b652c5863ed003510f54bf55347396232b8c23ec0532524efb03"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.33/tune-server-v0.8.33-linux-x86_64.tar.gz"
      sha256 "c85845ec30f44f86726626be38ef9156b03e205063b7f764f9c61fac12ca1501"
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
      Tune Server v0.8.33 (Rust) installed!

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
