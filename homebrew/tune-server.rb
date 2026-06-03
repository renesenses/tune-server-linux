class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.27"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.27/tune-server-v0.8.27-macos-aarch64.tar.gz"
      sha256 "c69c9192af567faf640bddbfb23b1c71dfc4e65d75b0804cc8e00e78bf999d6a"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.27/tune-server-v0.8.27-macos-x86_64.tar.gz"
      sha256 "b605217a4f0ca937c1598f4cbba74fc4099eeec337157e90e595a5722fd264ce"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.27/tune-server-v0.8.27-linux-aarch64.tar.gz"
      sha256 "180137f7927561ae762150c0e33ff0b0a2ec75e2949c4f93b782bbfd5f9019ba"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.27/tune-server-v0.8.27-linux-x86_64.tar.gz"
      sha256 "da306d024c6a8e9b86cccc4d3d49deaeafd348bc6a821a4ee1c22815394ae5a3"
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
      Tune Server v0.8.27 (Rust) installed!

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
