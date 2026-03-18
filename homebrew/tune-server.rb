class TuneServer < Formula
  desc "Multi-room music server with DLNA/UPnP, AirPlay, and streaming services"
  homepage "https://github.com/renesenses/tune-server-linux"
  version "0.1.5"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/renesenses/tune-server-linux/releases/download/v0.1.5/tune-server-0.1.5-macos.tar.gz"
      sha256 "b5b8512cc91b645f6603af1e6ab8486ca08546d0ed0d5d8f51ce37b36fcc90e9"
    else
      url "https://github.com/renesenses/tune-server-linux/releases/download/v0.1.5/tune-server-0.1.5-macos-intel.tar.gz"
      sha256 "de50098a3beae4111eeea00daf6d327498405153399551aa02710995ccc73d61"
    end
  end

  on_linux do
    url "https://github.com/renesenses/tune-server-linux/releases/download/v0.1.5/tune-server-0.1.5-linux.tar.gz"
    sha256 "b8e2f18f245202755a62cf6be7955e742dde4b13eb0d117acfa00941581172e3"
  end

  depends_on "python@3.12"
  depends_on "ffmpeg"
  depends_on "portaudio"

  def install
    # Create a virtualenv using the Homebrew Python
    venv = libexec/"venv"
    system Formula["python@3.12"].opt_bin/"python3.12", "-m", "venv", venv

    # Install the package and its dependencies into the venv
    system venv/"bin/pip", "install", "--no-cache-dir", "."

    # Create a wrapper script in Homebrew's bin
    (bin/"tune-server").write <<~EOS
      #!/bin/bash
      exec "#{venv}/bin/tune-server" "$@"
    EOS

    # Install example configuration
    etc.install ".env.example" => "tune-server.env.example" if File.exist?(".env.example")
  end

  def post_install
    (var/"tune-server").mkpath
  end

  def caveats
    <<~EOS
      To configure tune-server, copy the example config:
        cp #{etc}/tune-server.env.example ~/.config/tune-server/.env

      Then edit ~/.config/tune-server/.env with your settings.

      To start tune-server:
        tune-server

      To start as a background service:
        brew services start tune-server

      Release notes: https://github.com/renesenses/tune-server-linux/releases/tag/v0.1.5
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
    system bin/"tune-server", "--help" rescue nil
  end
end
