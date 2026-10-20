# Decode an exported MP4 with Windows Media Foundation (the system's own H.264/AAC decoders, independent of FFmpeg).
# Usage: powershell -File tests\mf_decode_check.ps1 <video.mp4> <out_dir> <seconds,seconds,...>
# Prints the duration and tracks Windows reads, saves a frame (JPEG) at each requested time, and transcodes the
# whole file through Media Foundation, which decodes every audio and video sample.
param([string]$Video, [string]$OutDir, [string]$Times)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskOp = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskOpProgress = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperationWithProgress`2' })[0]
function Await($op, [Type]$type) {
    $t = $asTaskOp.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
function AwaitProgress($op, [Type]$type, [Type]$progress) {
    $t = $asTaskOpProgress.MakeGenericMethod($type, $progress).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFolder, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Editing.MediaComposition, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Editing.MediaClip, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.MediaProperties.MediaEncodingProfile, Windows.Media.MediaProperties, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Transcoding.TranscodeFailureReason, Windows.Media.Transcoding, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.ImageStream, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $Video).Path)) ([Windows.Storage.StorageFile])
$clip = Await ([Windows.Media.Editing.MediaClip]::CreateFromFileAsync($file)) ([Windows.Media.Editing.MediaClip])
$v = $clip.GetVideoEncodingProperties()
"duration_s={0:F3}" -f $clip.OriginalDuration.TotalSeconds
"video={0} {1}x{2}" -f $v.Subtype, $v.Width, $v.Height
foreach ($a in $clip.EmbeddedAudioTracks) {
    $p = $a.GetAudioEncodingProperties()
    "audio={0} {1}Hz {2}ch" -f $p.Subtype, $p.SampleRate, $p.ChannelCount
}
$comp = New-Object Windows.Media.Editing.MediaComposition
[System.Collections.Generic.ICollection[Windows.Media.Editing.MediaClip]].GetMethod("Add").Invoke($comp.Clips, @($clip))
foreach ($s in $Times.Split(",")) {
    $ts = [TimeSpan]::FromSeconds([double]$s)
    $img = Await ($comp.GetThumbnailAsync($ts, [int]$v.Width, [int]$v.Height,
        [Windows.Media.Editing.VideoFramePrecision]::NearestFrame)) ([Windows.Graphics.Imaging.ImageStream])
    $path = Join-Path $OutDir ("mf_{0}.jpg" -f $s)
    $in = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($img)
    $out = [System.IO.File]::Create($path)
    $in.CopyTo($out); $out.Close(); $in.Close()
    "frame {0} -> {1}" -f $s, $path
}
$folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync((Resolve-Path $OutDir).Path)) ([Windows.Storage.StorageFolder])
$target = Await ($folder.CreateFileAsync("mf_transcoded.mp4", [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
$profile = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateMp4([Windows.Media.MediaProperties.VideoEncodingQuality]::HD720p)
$t0 = Get-Date
$reason = AwaitProgress ($comp.RenderToFileAsync($target, [Windows.Media.Editing.MediaTrimmingPreference]::Precise, $profile)) ([Windows.Media.Transcoding.TranscodeFailureReason]) ([double])
"transcode={0} in {1:F1}s -> {2}" -f $reason, ((Get-Date) - $t0).TotalSeconds, $target.Path
