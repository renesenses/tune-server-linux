class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.38"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.38/tune-server-v0.8.37-macos-aarch64.tar.gz"
      sha256 "48bbbfbb3f66f9370f022cc215cdab87fabd625ae19044a1b5236d7fdca471e6"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.38/tune-server-v0.8.37-macos-x86_64.tar.gz"
      sha256 "2693e82345dc0b56b4f695718d3d3012c041e854829ca7b2f019f53d26ec3470"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.38/tune-server-v0.8.37-linux-aarch64.tar.gz"
      sha256 "98192a616f660a51529536a6df85c9143c86bfd811f7fddc006f380d6799f6e0"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.38/tune-server-v0.8.37-linux-x86_64.tar.gz"
      sha256 "c3783719e33b144f4402cf377544e12aafb6fbe439a90a5905b4b940de891d85"
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
      Tune Server v0.8.38 (Rust) installed!

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
