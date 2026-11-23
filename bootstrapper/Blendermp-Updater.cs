using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text.RegularExpressions;
using System.Windows.Forms;

// Stable launcher: always downloads the newest installer from GitHub Releases,
// installs it silently, then starts the app. Friends keep one copy of this exe.
class UpdaterForm : Form
{
    private const string Owner = "__OWNER__";
    private const string Repo = "__REPO__";

    private readonly ProgressBar bar;
    private readonly Label status;

    public UpdaterForm()
    {
        Text = "Blendermp Updater";
        ClientSize = new Size(420, 132);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        ShowIcon = false;

        status = new Label
        {
            Left = 16,
            Top = 18,
            Width = 388,
            Height = 40,
            Text = "Checking for the latest Blendermp..."
        };
        bar = new ProgressBar
        {
            Left = 16,
            Top = 68,
            Width = 388,
            Height = 22,
            Minimum = 0,
            Maximum = 100
        };
        Controls.Add(status);
        Controls.Add(bar);
        Shown += async (sender, args) => await RunAsync();
    }

    private async System.Threading.Tasks.Task RunAsync()
    {
        try
        {
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
            var client = new WebClient();
            client.Headers.Add("User-Agent", "Blendermp-Updater");
            var api = string.Format("https://api.github.com/repos/{0}/{1}/releases/latest", Owner, Repo);
            var json = await client.DownloadStringTaskAsync(api);
            var match = Regex.Match(json, "\"browser_download_url\"\\s*:\\s*\"([^\"]*-Setup\\.exe)\"");
            if (!match.Success)
            {
                status.Text = "No installer was found in the latest release.";
                bar.Value = 0;
                return;
            }

            var url = match.Groups[1].Value;
            var temp = Path.Combine(Path.GetTempPath(), "Blendermp-Setup.exe");
            status.Text = "Downloading the latest installer...";
            client.DownloadProgressChanged += (sender, args) =>
            {
                bar.Value = Math.Max(0, Math.Min(100, args.ProgressPercentage));
            };
            await client.DownloadFileTaskAsync(new Uri(url), temp);

            status.Text = "Installing...";
            bar.Value = 100;
            var installer = Process.Start(new ProcessStartInfo(temp, "/S") { UseShellExecute = true });
            if (installer != null) installer.WaitForExit();

            var exe = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Programs", "Blendermp", "blendermp.exe");
            if (File.Exists(exe)) Process.Start(exe);
            Close();
        }
        catch (Exception ex)
        {
            MessageBox.Show("Update failed: " + ex.Message, "Blendermp", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }
}

static class Program
{
    [STAThread]
    static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new UpdaterForm());
    }
}
