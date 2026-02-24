class TuneServer < Formula
  desc "Multi-room music server with DLNA/UPnP, AirPlay, and streaming services"
  homepage "https://github.com/renesenses/tune-server"
  url "https://github.com/renesenses/tune-server/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "" # TODO: fill in after creating the release tarball
  license "MIT"

  depends_on "python@3.12"
  depends_on "ffmpeg"
  depends_on "portaudio"

  def install
    # Create a virtualenv using the Homebrew Python
    venv = libexec/"venv"
    system Formula["python@3.12"].opt_bin/"python3.12", "-m", "venv", venv

    # Install the package and its dependencies into the venv
    system venv/"bin/pip", "install", "--no-cache-dir", buildpath

    # Create a wrapper script in Homebrew's bin
    (bin/"tune-server").write <<~EOS
      #!/bin/bash
      exec "#{venv}/bin/tune-server" "$@"
    EOS

    # Install example configuration
    etc.install ".env.example" => "tune-server.env.example"
  end

  def post_install
    # Create a data directory for the server
    (var/"tune-server").mkpath
  end

  def caveats
    <<~EOS
      To configure tune-server, copy the example config:
        cp #{etc}/tune-server.env.example ~/.config/tune-server/.env

      Then edit ~/.config/tune-server/.env with your settings.

      To start tune-server manually:
        tune-server

      To start as a background service:
        brew services start tune-server
    EOS
  end

  service do
    run [opt_bin/"tune-server"]
    working_dir var/"tune-server"
    keep_alive true
    log_path var/"log/tune-server.log"
    error_log_path var/"log/tune-server.log"
    environment_variables PATH: std_service_path_env
  end

  test do
    # Verify the command is available and the module can be imported
    system bin/"tune-server", "--help" rescue nil
    system libexec/"venv/bin/python", "-c", "import tune_server"
  end
end
