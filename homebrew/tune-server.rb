class TuneServer < Formula
  desc "Multi-room music server with DLNA/UPnP, AirPlay, and streaming services"
  homepage "https://mozaiklabs.fr"
  url "https://github.com/renesenses/tune-server-linux/archive/refs/tags/v0.8.113.tar.gz"
  sha256 "a829b5e112cfdbc0b52b19846fd8aa6dbc1875e01c4007f33a8922c83f6eaca8"
  version "0.8.113"
  license "MIT"

  depends_on "node" => :build
  depends_on "python@3.11"
  depends_on "ffmpeg"
  depends_on "portaudio"

  resource "web-client" do
    url "https://github.com/renesenses/tune-web-client/archive/refs/tags/v0.8.113.tar.gz"
    sha256 "cd892b4db6f4ed665b474409915f8cedea43fdfbd180bb3f4be076dd82345cbd"
  end

  def install
    venv = libexec/"venv"
    system Formula["python@3.11"].opt_bin/"python3.11", "-m", "venv", venv
    system venv/"bin/pip", "install", "--no-cache-dir", "."

    resource("web-client").stage do
      system "npm", "install"
      system "npm", "run", "build"
      (libexec/"web").install Dir["dist/*"]
    end

    (bin/"tune-server").write <<~EOS
      #!/bin/bash
      export PATH="#{Formula["ffmpeg"].opt_bin}:$PATH"
      export TUNE_WEB_DIR="#{libexec}/web"
      exec "#{venv}/bin/tune-server" "$@"
    EOS
    chmod 0755, bin/"tune-server"
  end

  def post_install
    (var/"tune-server").mkpath
    (var/"tune-server/artwork_cache").mkpath
  end

  def caveats
    <<~EOS
      Tune Server v0.8.113 installed!

      Start the server:
        tune-server

      Then open http://localhost:8888 in your browser.

      Start as a background service:
        brew services start tune-server

      Configure music directories:
        Open http://localhost:8888 → Settings → Music Directories

      Release notes:
        https://github.com/renesenses/tune-server-linux/releases/tag/v0.7.86
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
    assert_match "tune", shell_output("#{bin}/tune-server --help 2>&1", 0)
  end
end
