class TuneServerPython < Formula
  desc "Multi-room music server (Python legacy) with DLNA/UPnP and streaming"
  homepage "https://mozaiklabs.fr"
  url "https://github.com/renesenses/tune-server-linux/archive/refs/tags/v0.7.132.tar.gz"
  sha256 "5af29f15bfeffc61764066b83c936a32e7922062508088f0bab081d061e1899a"
  version "0.7.132"
  license "MIT"

  depends_on "node" => :build
  depends_on "python@3.11"
  depends_on "ffmpeg"
  depends_on "portaudio"

  resource "web-client" do
    url "https://github.com/renesenses/tune-web-client/archive/refs/tags/v0.7.132.tar.gz"
    sha256 "d2165137fbfb4395fc7659420a8e90b569dbe0b7d56567e87e6476062d67564c"
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

    (bin/"tune-server-python").write <<~EOS
      #!/bin/bash
      export PATH="#{Formula["ffmpeg"].opt_bin}:$PATH"
      export TUNE_WEB_DIR="#{libexec}/web"
      exec "#{venv}/bin/tune-server" "$@"
    EOS
    chmod 0755, bin/"tune-server-python"
  end

  def caveats
    <<~EOS
      Tune Server v0.7.132 (Python legacy) installed!

      Start: tune-server-python
      Web UI: http://localhost:8888

      Recommended: upgrade to Rust version
        brew install renesenses/tap/tune-server
    EOS
  end

  service do
    run [opt_bin/"tune-server-python"]
    working_dir var/"tune-server"
    keep_alive true
    log_path var/"log/tune-server-python.log"
    error_log_path var/"log/tune-server-python.log"
    environment_variables PATH: std_service_path_env
  end

  test do
    assert_match "tune", shell_output("#{bin}/tune-server-python --help 2>&1", 0)
  end
end
