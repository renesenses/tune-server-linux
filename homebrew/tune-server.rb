class TuneServer < Formula
  desc "Multi-room music server (Rust) with DLNA/UPnP, streaming, and web UI"
  homepage "https://mozaiklabs.fr"
  version "0.8.0"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.0/tune-server-macos-aarch64.tar.gz"
      sha256 "39defdd01295e85fc30f5e29164ecde146a84cc3a2e3758e75e6f5887170d55b"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.0/tune-server-macos-x86_64.tar.gz"
      sha256 "58bba8f4b6cbeffbf1e448767e421a7ec193167348fabae736ceff8c82fea05f"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.0/tune-server-linux-aarch64.tar.gz"
      sha256 "8811bf357a0cc5784caf9e895813acffd68027f4475b09d51e3dbf55b7e16a91"
    else
      url "https://github.com/renesenses/tune-server-rust/releases/download/v0.8.0/tune-server-linux-x86_64.tar.gz"
      sha256 "87c5cbe886e555cb934c18ac88a153a6267ef382d5af3c36ede237db23681ddd"
    end
  end

  depends_on "ffmpeg"

  def install
    bin.install "tune-server"

    (bin/"tune-server-launcher").write <<~EOS
      #!/bin/bash
      export PATH="#{Formula["ffmpeg"].opt_bin}:$PATH"
      export TUNE_PORT="${TUNE_PORT:-8888}"
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
      Tune Server v0.8.0 (Rust) installed!

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
