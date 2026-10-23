
# 大眼 · OneCore 语音 worker（常驻，走 stdin/stdout 管道）
#
# 为什么常驻：Windows.Media.SpeechSynthesis 是 WinRT 异步 API，
# 冷启动要 Add-Type + 反射 AsTask，每次新建进程约 200~400ms；
# 常驻后单次合成只需 80~140ms，且不影响主进程（PS7 不支持 WinRT，必须 powershell.exe 5.1）。
#
# 协议（严格一行一请求，全部 UTF-8 base64，避免控制台编码踩坑）：
#   in : <text_b64>|<out_wav_path_b64>|<voice_name_b64>
#   out: OK|<elapsed_ms>|<bytes>|<voice_display_name>
#      | ERR|<message>
#   特殊命令 in: LIST  出: VOICES|<name1>;<name2>;...
$ErrorActionPreference = 'Continue'
try {
  [Console]::InputEncoding  = New-Object System.Text.UTF8Encoding $false
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
} catch {}
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
  $m = $asTaskGeneric.MakeGenericMethod($type)
  $t = $m.Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}

$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$voices = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices

function B64Decode($s) { [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($s)) }

Write-Output "READY"
[Console]::Out.Flush()

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($line -eq $null) { break }
  $line = $line.Trim()
  if ($line -eq '') { continue }
  try {
    $parts = $line.Split('|')
    if ($parts[0] -eq 'LIST') {
      $names = ($voices | ForEach-Object { $_.DisplayName }) -join ';'
      Write-Output ("VOICES|" + $names)
      [Console]::Out.Flush()
      continue
    }
    $text  = B64Decode $parts[0]
    $out   = B64Decode $parts[1]
    $vname = if ($parts.Length -gt 2 -and $parts[2]) { B64Decode $parts[2] } else { '' }
    $syn = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
    if ($vname) {
      foreach ($v in $voices) {
        if ($v.DisplayName -like ("*" + $vname + "*")) { $syn.Voice = $v; break }
      }
    }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $op = $syn.SynthesizeTextToStreamAsync($text)
    $stream = Await $op ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
    $size = [uint32]$stream.Size
    $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
    $loaded = Await ($reader.LoadAsync($size)) ([uint32])
    $bytes = New-Object byte[] $loaded
    $reader.ReadBytes($bytes)
    [System.IO.File]::WriteAllBytes($out, $bytes)
    $sw.Stop()
    Write-Output ("OK|" + $sw.ElapsedMilliseconds + "|" + $bytes.Length + "|" + $syn.Voice.DisplayName)
  } catch {
    Write-Output ("ERR|" + ($_.Exception.Message -replace "[\r\n\|]", " "))
  }
  [Console]::Out.Flush()
}
