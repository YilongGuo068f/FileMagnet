# 多机位素材筛选拷贝工具 —— PowerShell 引擎
# 用于没有安装 Python 的 Windows 电脑，只依赖系统自带的 Windows PowerShell 5.1（Windows 10/11 均自带）。
# 与 server.py 提供完全相同的接口，网页控制台无需区分。
#
# 用法:
#   powershell -NoProfile -STA -ExecutionPolicy Bypass -File server.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File server.ps1 -Run 方案.json [-Preview]
param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [string]$Run = "",
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Off

$script:ToolVersion = '1.0.0'
$script:EngineDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:ToolDir = Split-Path -Parent $script:EngineDir
$script:AppFile = [IO.Path]::Combine($script:ToolDir, 'app', 'index.html')
$script:PresetDir = [IO.Path]::Combine($script:ToolDir, 'presets')
$script:LogDir = [IO.Path]::Combine($script:ToolDir, 'logs')
$script:OnWin = ([Environment]::OSVersion.Platform -eq 'Win32NT')
$script:OnMac = (-not $script:OnWin) -and (Test-Path '/System/Library/CoreServices')
$script:Sep = [string][IO.Path]::DirectorySeparatorChar
$script:Inv = [Globalization.CultureInfo]::InvariantCulture
$script:Latin1 = [Text.Encoding]::GetEncoding(28591)
$script:Utf8 = New-Object Text.UTF8Encoding($false)
$script:J = $null
$script:Quit = $false
$script:HiddenNames = @('thumbs.db', 'desktop.ini', '.ds_store', '$recycle.bin', 'system volume information', '.spotlight-v100', '.trashes', '.fseventsd')
$script:FieldLabel = @{ created = '创建时间'; modified = '修改时间'; exif = '拍摄时间(EXIF)' }
$script:ResultLabel = @{ copied = '已拷贝'; moved = '已移动'; skipped = '已跳过'; failed = '失败'; new = '将拷贝'; exists = '目标已存在' }

$rng = New-Object Security.Cryptography.RNGCryptoServiceProvider
$tb = New-Object byte[] 18
$rng.GetBytes($tb)
$script:Token = [Convert]::ToBase64String($tb).Replace('+', '-').Replace('/', '_').TrimEnd('=')

# ---------------------------------------------------------------- 通用
function UErr([string]$m) { throw ('USERERR:' + $m) }
function New-List { return , (New-Object 'System.Collections.Generic.List[object]') }
function Fmt-Dt($dt) { if ($null -eq $dt) { return '' }; return ([datetime]$dt).ToString('yyyy-MM-dd HH:mm:ss', $script:Inv) }
function Now-Str { return (Get-Date).ToString('HH:mm:ss', $script:Inv) }

function Err-Msg($e) {
    $ex = $e.Exception
    while ($ex -is [Management.Automation.MethodInvocationException] -and $null -ne $ex.InnerException) { $ex = $ex.InnerException }
    $m = $ex.Message
    if ($m.StartsWith('USERERR:')) { return $m.Substring(8) }
    return $m
}

function Is-UserErr($e) { return $e.Exception.Message.StartsWith('USERERR:') }

function Cfg($obj, [string]$path, $default = $null) {
    $cur = $obj
    foreach ($k in $path.Split('.')) {
        if ($null -eq $cur) { return $default }
        if ($cur -is [Collections.IDictionary]) { if (-not $cur.Contains($k)) { return $default }; $cur = $cur[$k]; continue }
        $p = $cur.PSObject.Properties[$k]
        if ($null -eq $p) { return $default }
        $cur = $p.Value
    }
    if ($null -eq $cur) { return $default }
    return $cur
}

function Merge-Defaults($cfg, $def) {
    if ($null -eq $cfg) { $cfg = New-Object PSObject }
    foreach ($p in $def.PSObject.Properties) {
        $have = $cfg.PSObject.Properties[$p.Name]
        if ($null -eq $have) {
            $cfg | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
        }
        elseif ($p.Value -is [PSCustomObject] -and $have.Value -is [PSCustomObject] -and $p.Name -ne 'cameraOverrides') {
            [void](Merge-Defaults $have.Value $p.Value)
        }
    }
    return $cfg
}

function Load-Defaults {
    $f = [IO.Path]::Combine($script:EngineDir, 'defaults.json')
    return ([IO.File]::ReadAllText($f, [Text.Encoding]::UTF8) | ConvertFrom-Json)
}
$script:DefaultsJson = [IO.File]::ReadAllText([IO.Path]::Combine($script:EngineDir, 'defaults.json'), [Text.Encoding]::UTF8)

function Norm-Path([string]$p) {
    if ($null -eq $p) { return '' }
    $p = $p.Trim().Trim('"').Trim("'").Trim()
    if ($p -eq '') { return '' }
    if ($p -eq '~' -or $p.StartsWith('~/') -or $p.StartsWith('~\')) {
        $p = [Environment]::GetFolderPath('UserProfile') + $p.Substring(1)
    }
    $p = [IO.Path]::GetFullPath($p)
    $root = [IO.Path]::GetPathRoot($p)
    if ($p.Length -gt $root.Length) { $p = $p.TrimEnd('\', '/') }
    return $p
}

function Is-Inside([string]$child, [string]$parent) {
    $cmp = if ($script:OnWin) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $c = $child.TrimEnd('\', '/'); $p = $parent.TrimEnd('\', '/')
    if ([string]::Equals($c, $p, $cmp)) { return $true }
    return $c.StartsWith($p + $script:Sep, $cmp)
}

function Sanitize([string]$s) {
    $s = ($s -replace '[<>:"|?*\x00-\x1f]', '_').Trim()
    return $s.TrimEnd('.', ' ')
}

function Split-Patterns($text) {
    $out = New-List
    if ($null -eq $text) { return , $out }
    foreach ($p in ([string]$text -split '[;\r\n]+')) { $t = $p.Trim(); if ($t) { $out.Add($t) } }
    return , $out
}

function Csv-Line($fields) {
    return (($fields | ForEach-Object { '"' + ([string]$_).Replace('"', '""') + '"' }) -join ',')
}

# ---------------------------------------------------------------- 系统识别
function System-Info {
    $info = [ordered]@{
        engine = 'PowerShell'; engineVersion = $PSVersionTable.PSVersion.ToString(); toolVersion = $script:ToolVersion
        sep = $script:Sep; home = [Environment]::GetFolderPath('UserProfile'); toolDir = $script:ToolDir; logDir = $script:LogDir
        arch = $(if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' })
    }
    if ($script:OnWin) {
        $build = [Environment]::OSVersion.Version.Build
        $disp = ''
        try { $disp = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop).DisplayVersion } catch { }
        if ($build -ge 22000) { $name = 'Windows 11' } elseif ($build -ge 10240) { $name = 'Windows 10' } else { $name = 'Windows ' + [Environment]::OSVersion.Version.ToString() }
        $label = $name + $(if ($disp) { ' ' + $disp } else { '' }) + ' (内部版本 ' + $build + ')'
        $info.os = 'windows'; $info.osName = $name; $info.osLabel = $label; $info.supportsCreated = $true
    }
    elseif ($script:OnMac) {
        $v = ''
        try { $v = (& sw_vers -productVersion) } catch { }
        $info.os = 'macos'; $info.osName = 'macOS'; $info.osLabel = ('macOS ' + $v).Trim(); $info.supportsCreated = $true
    }
    else {
        $pretty = 'Linux'
        try { foreach ($l in [IO.File]::ReadAllLines('/etc/os-release')) { if ($l.StartsWith('PRETTY_NAME=')) { $pretty = $l.Substring(12).Trim('"') } } } catch { }
        $info.os = 'linux'; $info.osName = 'Linux'; $info.osLabel = $pretty; $info.supportsCreated = $false
    }
    return $info
}

# ---------------------------------------------------------------- EXIF
function U16([byte[]]$b, [int]$o, [bool]$le) {
    if ($o -lt 0 -or $o + 2 -gt $b.Length) { return 0 }
    if ($le) { return ([int]$b[$o]) -bor ([int]$b[$o + 1] -shl 8) }
    return ([int]$b[$o] -shl 8) -bor ([int]$b[$o + 1])
}
function U32([byte[]]$b, [int64]$o, [bool]$le) {
    if ($o -lt 0 -or $o + 4 -gt $b.Length) { return [int64]0 }
    $a = [int64]$b[$o]; $c = [int64]$b[$o + 1]; $d = [int64]$b[$o + 2]; $e = [int64]$b[$o + 3]
    if ($le) { return $a + ($c -shl 8) + ($d -shl 16) + ($e -shl 24) }
    return $e + ($d -shl 8) + ($c -shl 16) + ($a -shl 24)
}

function Parse-ExifText([string]$s) {
    if ($s -match '^\s*(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})') {
        try { return New-Object DateTime([int]$Matches[1], [int]$Matches[2], [int]$Matches[3], [int]$Matches[4], [int]$Matches[5], [int]$Matches[6]) } catch { return $null }
    }
    return $null
}

function Read-Ifd([byte[]]$buf, [int64]$base, [int64]$off, [bool]$le, $want) {
    $res = @{}
    $p = $base + $off
    if ($off -le 0 -or $p + 2 -gt $buf.Length) { return $res }
    $n = U16 $buf $p $le
    if ($n -gt 2000) { return $res }
    for ($i = 0; $i -lt $n; $i++) {
        $e = $p + 2 + $i * 12
        if ($e + 12 -gt $buf.Length) { break }
        $tag = U16 $buf $e $le
        if ($want -notcontains $tag) { continue }
        $typ = U16 $buf ($e + 2) $le
        $cnt = U32 $buf ($e + 4) $le
        if ($typ -eq 2) {
            if ($cnt -le 4) { $vo = $e + 8 } else { $vo = $base + (U32 $buf ($e + 8) $le) }
            $len = [Math]::Min([int64]$cnt, [int64]($buf.Length - $vo))
            if ($vo -ge 0 -and $len -gt 0) { $res[$tag] = [Text.Encoding]::ASCII.GetString($buf, [int]$vo, [int]$len) }
        }
        elseif ($typ -eq 4 -or $typ -eq 13) { $res[$tag] = [int64](U32 $buf ($e + 8) $le) }
    }
    return $res
}

function Parse-Tiff([byte[]]$buf, [int64]$base) {
    if ($base + 8 -gt $buf.Length) { return $null }
    if ($buf[$base] -eq 0x49 -and $buf[$base + 1] -eq 0x49) { $le = $true }
    elseif ($buf[$base] -eq 0x4D -and $buf[$base + 1] -eq 0x4D) { $le = $false }
    else { return $null }
    $ifd0 = Read-Ifd $buf $base (U32 $buf ($base + 4) $le) $le @(0x0132, 0x8769, 0x9003, 0x9004)
    $exif = @{}
    if ($ifd0.ContainsKey(0x8769) -and $ifd0[0x8769] -is [int64]) { $exif = Read-Ifd $buf $base $ifd0[0x8769] $le @(0x9003, 0x9004) }
    foreach ($pair in @(@(0x9003, $exif), @(0x9003, $ifd0), @(0x9004, $exif), @(0x9004, $ifd0), @(0x0132, $ifd0))) {
        $src = $pair[1]; $tag = $pair[0]
        if ($src.ContainsKey($tag) -and $src[$tag] -is [string]) {
            $dt = Parse-ExifText $src[$tag]
            if ($null -ne $dt) { return $dt }
        }
    }
    return $null
}

function Read-ExifDate([string]$path) {
    try {
        $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
        try {
            $first = New-Object byte[] 2
            [void]$fs.Read($first, 0, 2)
            $want = if ($first[0] -eq 0xFF -and $first[1] -eq 0xD8) { 131072 } else { 524288 }
            $n = [int][Math]::Min([int64]$want, $fs.Length)
            $buf = New-Object byte[] $n
            $fs.Position = 0
            $got = 0
            while ($got -lt $n) { $r = $fs.Read($buf, $got, $n - $got); if ($r -le 0) { break }; $got += $r }
        }
        finally { $fs.Dispose() }
    }
    catch { return $null }
    if ($buf.Length -lt 4) { return $null }
    if ($buf[0] -eq 0xFF -and $buf[1] -eq 0xD8) {
        $i = 2
        while ($i + 4 -le $buf.Length) {
            if ($buf[$i] -ne 0xFF) { break }
            $m = $buf[$i + 1]
            if ($m -eq 0xFF) { $i += 1; continue }
            if ($m -eq 0xD8 -or $m -eq 0x01 -or ($m -ge 0xD0 -and $m -le 0xD7)) { $i += 2; continue }
            if ($m -eq 0xDA) { break }
            $seg = U16 $buf ($i + 2) $false
            if ($m -eq 0xE1 -and $i + 10 -le $buf.Length -and $script:Latin1.GetString($buf, $i + 4, 6) -eq "Exif`0`0") {
                $r = Parse-Tiff $buf ($i + 10)
                if ($null -ne $r) { return $r }
            }
            $i += 2 + $seg
        }
    }
    elseif (($buf[0] -eq 0x49 -and $buf[1] -eq 0x49) -or ($buf[0] -eq 0x4D -and $buf[1] -eq 0x4D)) {
        $r = Parse-Tiff $buf 0
        if ($null -ne $r) { return $r }
    }
    $text = $script:Latin1.GetString($buf)
    foreach ($mk in @(@("Exif`0`0", 6), @('CMT2', 4), @('CMT1', 4))) {
        $idx = $text.IndexOf($mk[0], [StringComparison]::Ordinal)
        if ($idx -ge 0) { $r = Parse-Tiff $buf ($idx + $mk[1]); if ($null -ne $r) { return $r } }
    }
    $mm = [regex]::Match($text, '(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})')
    if ($mm.Success) { return (Parse-ExifText $mm.Value) }
    return $null
}

function Get-FileTime($fi, [string]$field, [string]$fallback) {
    $supportsCreated = $script:OnWin -or $script:OnMac
    if ($field -eq 'modified') { return @{ dt = $fi.LastWriteTime; src = '修改' } }
    if ($field -eq 'created') {
        if (-not $supportsCreated) { return @{ dt = $fi.LastWriteTime; src = '修改(系统无创建时间)' } }
        return @{ dt = $fi.CreationTime; src = '创建' }
    }
    $d = Read-ExifDate $fi.FullName
    if ($null -ne $d) { return @{ dt = $d; src = 'EXIF' } }
    if ($fallback -eq 'created') {
        if ($supportsCreated) { return @{ dt = $fi.CreationTime; src = '创建(无EXIF)' } }
        return @{ dt = $fi.LastWriteTime; src = '修改(无EXIF)' }
    }
    if ($fallback -eq 'modified') { return @{ dt = $fi.LastWriteTime; src = '修改(无EXIF)' } }
    return @{ dt = $null; src = '无EXIF' }
}

# ---------------------------------------------------------------- 筛选条件
function Parse-Tod($s, [bool]$isEnd) {
    $s = [string]$s
    if ($s -notmatch '^\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.\d+)?\s*$') { UErr ("时间格式不正确：'" + $s + "'（应为 时:分 或 时:分:秒）") }
    $h = [int]$Matches[1]; $mi = [int]$Matches[2]; $hasSec = $Matches.ContainsKey(3) -and $Matches[3]
    $se = if ($hasSec) { [int]$Matches[3] } else { 0 }
    if ($h -gt 23 -or $mi -gt 59 -or $se -gt 59) { UErr ("时间超出范围：'" + $s + "'") }
    $secs = $h * 3600 + $mi * 60 + $se
    if ($isEnd) { if ($hasSec) { $secs += 1 } else { $secs += 60 } }
    return $secs
}

function Parse-Dt($s, [bool]$isEnd) {
    if ($s -is [datetime]) {
        # PowerShell 7 的 ConvertFrom-Json 会把 ISO 日期自动转成 DateTime，这里转回文本统一处理
        $fmt = if ($s.Second -ne 0) { 'yyyy-MM-dd HH:mm:ss' } elseif ($s.TimeOfDay.Ticks -eq 0) { 'yyyy-MM-dd' } else { 'yyyy-MM-dd HH:mm' }
        $s = $s.ToString($fmt, $script:Inv)
    }
    $s = ([string]$s).Trim()
    if ($s -eq '') { return $null }
    if ($s -notmatch '^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.\d+)?)?$') { UErr ("日期时间格式不正确：'" + $s + "'（应为 2026-10-14 11:38:00）") }
    $hasTime = $Matches.ContainsKey(4) -and $Matches[4]
    $hasSec = $Matches.ContainsKey(6) -and $Matches[6]
    try {
        $dt = New-Object DateTime([int]$Matches[1], [int]$Matches[2], [int]$Matches[3],
            $(if ($hasTime) { [int]$Matches[4] } else { 0 }), $(if ($hasTime) { [int]$Matches[5] } else { 0 }), $(if ($hasSec) { [int]$Matches[6] } else { 0 }))
    }
    catch { UErr ("日期时间无效：'" + $s + "'") }
    if ($isEnd) {
        if (-not $hasTime) { $dt = $dt.AddDays(1) } elseif (-not $hasSec) { $dt = $dt.AddSeconds(60) } else { $dt = $dt.AddSeconds(1) }
    }
    return $dt
}

function New-TimeFilter($t, [datetime]$now) {
    $tf = @{ enabled = [bool](Cfg $t 'enabled' $false); mode = [string](Cfg $t 'mode' 'daily'); desc = '' }
    if (-not $tf.enabled) { return $tf }
    if ($tf.mode -eq 'daily') {
        $dm = [string](Cfg $t 'dateMode' 'any')
        $tf.date = $null
        if ($dm -eq 'today') { $tf.date = $now.Date }
        elseif ($dm -eq 'fixed') {
            $d = Parse-Dt (Cfg $t 'date' '') $false
            if ($null -eq $d) { UErr '请填写指定日期' }
            $tf.date = $d.Date
        }
        $tf.wins = New-List
        foreach ($w in @(Cfg $t 'windows' @())) {
            if ($null -eq $w) { continue }
            $ws = [string](Cfg $w 'start' ''); $we = [string](Cfg $w 'end' '')
            if ($ws.Trim() -eq '' -and $we.Trim() -eq '') { continue }
            if ($ws.Trim() -eq '') { $ws = '00:00:00' }
            if ($we.Trim() -eq '') { $we = '23:59:59' }
            $tf.wins.Add(@{ s = (Parse-Tod $ws $false); e = (Parse-Tod $we $true); a = $ws; b = $we })
        }
        if ($tf.wins.Count -eq 0) { UErr '请至少填写一个时间段' }
        $parts = @($tf.wins | ForEach-Object { $_.a + '~' + $_.b })
        if ($null -ne $tf.date) { $tf.desc = $tf.date.ToString('yyyy-MM-dd', $script:Inv) + ' 当天 ' + ($parts -join '，') }
        else { $tf.desc = '每天 ' + ($parts -join '，') }
    }
    elseif ($tf.mode -eq 'range') {
        $tf.ranges = New-List
        foreach ($r in @(Cfg $t 'ranges' @())) {
            if ($null -eq $r) { continue }
            $s = Parse-Dt (Cfg $r 'start' '') $false
            $e = Parse-Dt (Cfg $r 'end' '') $true
            if ($null -eq $s -and $null -eq $e) { continue }
            if ($null -ne $s -and $null -ne $e -and $e -le $s) { UErr ('结束时间必须晚于开始时间：' + (Cfg $r 'start' '') + ' ~ ' + (Cfg $r 'end' '')) }
            $tf.ranges.Add(@{ s = $s; e = $e })
        }
        if ($tf.ranges.Count -eq 0) { UErr '请至少填写一个日期时间范围' }
        $parts = @($tf.ranges | ForEach-Object { $(if ($null -ne $_.s) { Fmt-Dt $_.s } else { '不限' }) + '~' + $(if ($null -ne $_.e) { Fmt-Dt $_.e } else { '不限' }) })
        $tf.desc = $parts -join '，'
    }
    elseif ($tf.mode -eq 'recent') {
        $amt = [double](Cfg $t 'recentAmount' 0)
        if ($amt -le 0) { UErr '“最近”的时长必须大于 0' }
        $unit = [string](Cfg $t 'recentUnit' 'minutes')
        $mult = switch ($unit) { 'hours' { 3600 } 'days' { 86400 } default { 60 } }
        $tf.since = $now.AddSeconds(- $amt * $mult)
        $tf.desc = (Fmt-Dt $tf.since) + ' 之后'
    }
    else { UErr ('未知的时间模式：' + $tf.mode) }
    return $tf
}

function Time-Match($tf, $dt) {
    if (-not $tf.enabled) { return $true }
    if ($null -eq $dt) { return $false }
    if ($tf.mode -eq 'daily') {
        if ($null -ne $tf.date -and $dt.Date -ne $tf.date) { return $false }
        $tod = $dt.TimeOfDay.TotalSeconds
        foreach ($w in $tf.wins) {
            if ($w.s -lt $w.e) { if ($tod -ge $w.s -and $tod -lt $w.e) { return $true } }
            else { if ($tod -ge $w.s -or $tod -lt $w.e) { return $true } }
        }
        return $false
    }
    if ($tf.mode -eq 'range') {
        foreach ($r in $tf.ranges) {
            if (($null -eq $r.s -or $dt -ge $r.s) -and ($null -eq $r.e -or $dt -lt $r.e)) { return $true }
        }
        return $false
    }
    return ($dt -ge $tf.since)
}

function New-Filters($cfg, [datetime]$now) {
    $f = @{}
    if ((Cfg $cfg 'ext.mode' 'list') -eq 'all') { $f.exts = $null }
    else {
        $f.exts = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($x in @(Cfg $cfg 'ext.list' @())) { if ($x) { $v = ([string]$x).Trim().ToLowerInvariant().TrimStart('.'); if ($v) { [void]$f.exts.Add($v) } } }
        if ($f.exts.Count -eq 0) { UErr '没有选择任何文件类型' }
    }
    $f.regex = [bool](Cfg $cfg 'name.regex' $false)
    $f.inc = Split-Patterns (Cfg $cfg 'name.include' '')
    $f.exc = Split-Patterns (Cfg $cfg 'name.exclude' '')
    if ($f.regex) {
        foreach ($p in @($f.inc) + @($f.exc)) {
            try { [void](New-Object Text.RegularExpressions.Regex($p, 'IgnoreCase')) } catch { UErr ('正则表达式有误：' + $p) }
        }
    }
    $mn = Cfg $cfg 'size.minMB' $null; $mx = Cfg $cfg 'size.maxMB' $null
    $f.min = if ($null -ne $mn -and [string]$mn -ne '') { [double]$mn * 1048576 } else { $null }
    $f.max = if ($null -ne $mx -and [string]$mx -ne '') { [double]$mx * 1048576 } else { $null }
    $f.skipHidden = [bool](Cfg $cfg 'skipHidden' $true)
    $f.time = New-TimeFilter (Cfg $cfg 'time' $null) $now
    return $f
}

function Name-Hit($f, [string]$name, $pats) {
    foreach ($p in $pats) {
        if ($f.regex) { if ([regex]::IsMatch($name, $p, 'IgnoreCase')) { return $true } }
        else { if ($name -like $p) { return $true } }
    }
    return $false
}

function Pre-Match($f, [string]$name, [int64]$size) {
    if ($null -ne $f.exts) {
        $ext = [IO.Path]::GetExtension($name).ToLowerInvariant().TrimStart('.')
        if (-not $f.exts.Contains($ext)) { return $false }
    }
    if ($f.inc.Count -gt 0 -and -not (Name-Hit $f $name $f.inc)) { return $false }
    if ($f.exc.Count -gt 0 -and (Name-Hit $f $name $f.exc)) { return $false }
    if ($null -ne $f.min -and $size -lt $f.min) { return $false }
    if ($null -ne $f.max -and $size -gt $f.max) { return $false }
    return $true
}

# ---------------------------------------------------------------- 机位与文件
function Is-HiddenItem($fsi) {
    $n = $fsi.Name
    if ($n.StartsWith('.') -or $script:HiddenNames -contains $n.ToLowerInvariant()) { return $true }
    if ($script:OnWin) {
        try { return [bool](($fsi.Attributes -band [IO.FileAttributes]::Hidden) -or ($fsi.Attributes -band [IO.FileAttributes]::System)) } catch { return $false }
    }
    return $false
}

function Discover-Cameras($cfg, [bool]$checkTarget) {
    $cams = New-List; $errors = New-List
    $pairs = @(Cfg $cfg 'pairs' @())
    if ($pairs.Count -eq 0) { UErr '请至少设置一组“源目录 → 目标目录”' }
    $ov = Cfg $cfg 'cameraOverrides' $null
    $inc = Split-Patterns (Cfg $cfg 'cameraInclude' '')
    $exc = Split-Patterns (Cfg $cfg 'cameraExclude' '')
    $mode = [string](Cfg $cfg 'cameraMode' 'subfolders')
    for ($pi = 0; $pi -lt $pairs.Count; $pi++) {
        $pair = $pairs[$pi]
        $label = '第 ' + ($pi + 1) + ' 组'
        try { $src = Norm-Path (Cfg $pair 'source' ''); $tgt = Norm-Path (Cfg $pair 'target' '') }
        catch { $errors.Add($label + '：路径格式不正确'); continue }
        if (-not $src) { $errors.Add($label + '：未填写源目录'); continue }
        if (-not [IO.Directory]::Exists($src)) { $errors.Add($label + '：源目录不存在或无法访问：' + $src); continue }
        if ($checkTarget) {
            if (-not $tgt) { $errors.Add($label + '：未填写目标目录'); continue }
            if (Is-Inside $tgt $src) { $errors.Add($label + '：目标目录不能是源目录本身或位于源目录内部'); continue }
        }
        if ($mode -eq 'root') {
            $name = [IO.Path]::GetFileName($src); if (-not $name) { $name = $src }
            $cams.Add((New-Camera $pi $src $name $src $tgt $ov))
            continue
        }
        try { $dirs = @((New-Object IO.DirectoryInfo($src)).GetDirectories() | Sort-Object { $_.Name.ToLowerInvariant() }) }
        catch { $errors.Add($label + '：无法读取源目录：' + (Err-Msg $_)); continue }
        $found = 0
        foreach ($d in $dirs) {
            if (Is-HiddenItem $d) { continue }
            if ($inc.Count -gt 0) { $ok = $false; foreach ($p in $inc) { if ($d.Name -like $p) { $ok = $true } }; if (-not $ok) { continue } }
            if ($exc.Count -gt 0) { $bad = $false; foreach ($p in $exc) { if ($d.Name -like $p) { $bad = $true } }; if ($bad) { continue } }
            $cams.Add((New-Camera $pi $src $d.Name $d.FullName $tgt $ov))
            $found++
        }
        if ($found -eq 0) { $errors.Add($label + '：源目录下没有找到机位子文件夹：' + $src) }
    }
    return @{ cams = $cams; errors = $errors }
}

function New-Camera($pi, $src, $name, $path, $tgt, $ov) {
    $key = $src + '::' + $name
    $o = $null
    if ($null -ne $ov) { $p = $ov.PSObject.Properties[$key]; if ($null -ne $p) { $o = $p.Value } }
    $enabled = $true
    $tn = ''
    if ($null -ne $o) {
        if ((Cfg $o 'enabled' $true) -eq $false) { $enabled = $false }
        $tn = ([string](Cfg $o 'target' '')).Trim()
    }
    if (-not $tn) { $tn = $name }
    return @{ pair = $pi; source = $src; name = $name; path = $path; targetRoot = $tgt; key = $key; enabled = $enabled; targetName = $tn }
}

function Get-CamFiles([string]$root, [bool]$recursive, [bool]$skipHidden, $onError) {
    $out = New-List
    $stack = New-Object 'System.Collections.Generic.Stack[object]'
    $stack.Push(@((New-Object IO.DirectoryInfo($root)), ''))
    while ($stack.Count -gt 0) {
        $top = $stack.Pop(); $di = $top[0]; $rel = $top[1]
        try { $entries = $di.GetFileSystemInfos() }
        catch { if ($null -ne $onError) { & $onError ('无法读取文件夹 ' + $di.FullName + '：' + (Err-Msg $_)) }; continue }
        foreach ($e in $entries) {
            if ($skipHidden -and (Is-HiddenItem $e)) { continue }
            $r = if ($rel) { $rel + $script:Sep + $e.Name } else { $e.Name }
            if ($e -is [IO.DirectoryInfo]) {
                if ($recursive -and -not ($e.Attributes -band [IO.FileAttributes]::ReparsePoint)) { $stack.Push(@($e, $r)) }
            }
            else { $out.Add(@{ fi = $e; rel = $r; full = $e.FullName }) }
        }
    }
    $sorted = New-List
    foreach ($x in ($out | Sort-Object { $_.rel.ToLowerInvariant() })) { $sorted.Add($x) }
    return , $sorted
}

function Render-TargetDir($cfg, $cam, $fileDt, [datetime]$runStart) {
    $tpl = ([string](Cfg $cfg 'targetTemplate' '{camera}')).Trim()
    if (-not $tpl) { $tpl = '{camera}' }
    $fd = if ($null -ne $fileDt) { [datetime]$fileDt } else { $runStart }
    $srcName = [IO.Path]::GetFileName($cam.source); if (-not $srcName) { $srcName = 'source' }
    $vals = [ordered]@{
        camera = $cam.targetName; source = $srcName
        date = $runStart.ToString('yyyyMMdd', $script:Inv); time = $runStart.ToString('HHmmss', $script:Inv)
        fileDate = $fd.ToString('yyyyMMdd', $script:Inv); fileHour = $fd.ToString('HH', $script:Inv)
    }
    $s = $tpl
    foreach ($k in $vals.Keys) { $s = $s.Replace('{' + $k + '}', [string]$vals[$k]) }
    $dir = $cam.targetRoot
    $n = 0
    foreach ($part in ($s -split '[\\/]+')) {
        $p = Sanitize $part
        if ($p -and $p -ne '.' -and $p -ne '..') { $dir = [IO.Path]::Combine($dir, $p); $n++ }
    }
    if ($n -eq 0) { $p = Sanitize $cam.targetName; if (-not $p) { $p = 'camera' }; $dir = [IO.Path]::Combine($dir, $p) }
    return $dir
}

# ---------------------------------------------------------------- 拷贝
function Same-File($it, [string]$dst) {
    try { $d = New-Object IO.FileInfo($dst) } catch { return $false }
    if (-not $d.Exists) { return $false }
    return ($d.Length -eq $it.size -and [Math]::Abs(($d.LastWriteTime - $it.mtime).TotalSeconds) -lt 2.1)
}

function Unique-Path([string]$p) {
    $dir = [IO.Path]::GetDirectoryName($p); $base = [IO.Path]::GetFileNameWithoutExtension($p); $ext = [IO.Path]::GetExtension($p)
    for ($i = 1; $i -lt 100000; $i++) {
        $c = [IO.Path]::Combine($dir, $base + '_' + $i + $ext)
        if (-not [IO.File]::Exists($c)) { return $c }
    }
    throw '无法生成不重复的文件名'
}

function Predict-Conflict($it, [string]$dst, [string]$conflict) {
    if (-not [IO.File]::Exists($dst)) { return @('new', '') }
    switch ($conflict) {
        'skip' { return @('exists', '目标已存在，将跳过') }
        'overwrite' { return @('exists', '目标已存在，将覆盖') }
        'newer' {
            if ($it.mtime -gt [IO.File]::GetLastWriteTime($dst).AddSeconds(1)) { return @('exists', '目标较旧，将覆盖') }
            return @('exists', '目标相同或更新，将跳过')
        }
    }
    if (Same-File $it $dst) { return @('exists', '目标已有相同文件，将跳过') }
    return @('exists', '目标已有同名文件，将自动改名')
}

function Transfer($it, [string]$dst, $cfg) {
    $conflict = [string](Cfg $cfg 'conflict' 'rename')
    $move = ((Cfg $cfg 'action' 'copy') -eq 'move')
    $src = $it.src
    if ([IO.File]::Exists($dst)) {
        if ($conflict -eq 'skip') { return @{ result = 'skipped'; note = '目标已存在'; dest = $dst } }
        if ($conflict -eq 'newer') {
            if ($it.mtime -le [IO.File]::GetLastWriteTime($dst).AddSeconds(1)) { return @{ result = 'skipped'; note = '目标相同或更新'; dest = $dst } }
        }
        elseif ($conflict -eq 'rename') {
            if (Same-File $it $dst) { return @{ result = 'skipped'; note = '目标已有相同文件'; dest = $dst } }
            $dst = Unique-Path $dst
        }
    }
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dst))
    if ($move -and -not [IO.File]::Exists($dst)) {
        $sameRoot = [string]::Equals([IO.Path]::GetPathRoot($src), [IO.Path]::GetPathRoot($dst), [StringComparison]::OrdinalIgnoreCase)
        if ($sameRoot) {
            try { [IO.File]::Move($src, $dst); $script:J.bytesDone += $it.size; return @{ result = 'moved'; note = ''; dest = $dst } } catch { }
        }
    }
    $tmp = $dst + '.mcpart'
    try {
        [IO.File]::Copy($src, $tmp, $true)
        try {
            if ([bool](Cfg $cfg 'preserveTimes' $true)) {
                if ($script:OnWin -or $script:OnMac) { [IO.File]::SetCreationTime($tmp, $it.ctime) }
                [IO.File]::SetLastWriteTime($tmp, $it.mtime)
            }
            else { $n = Get-Date; [IO.File]::SetLastWriteTime($tmp, $n); if ($script:OnWin) { [IO.File]::SetCreationTime($tmp, $n) } }
        }
        catch { }
        if ([bool](Cfg $cfg 'verify' $true)) {
            $got = (New-Object IO.FileInfo($tmp)).Length
            if ($got -ne $it.size) { throw ('校验失败：大小不一致（源 ' + $it.size + '，目标 ' + $got + '）') }
        }
        if ([IO.File]::Exists($dst)) { [IO.File]::Delete($dst) }
        [IO.File]::Move($tmp, $dst)
    }
    catch {
        try { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } } catch { }
        throw
    }
    $script:J.bytesDone += $it.size
    if ($move) { [IO.File]::Delete($src); return @{ result = 'moved'; note = '跨磁盘：已拷贝后删除源文件'; dest = $dst } }
    return @{ result = 'copied'; note = ''; dest = $dst }
}

# ---------------------------------------------------------------- 任务（分步执行，便于在单线程中同时响应网页）
function Job-Log([string]$level, [string]$msg) {
    $J = $script:J
    $J.logSeq++
    $J.logs.Add([ordered]@{ seq = $J.logSeq; t = (Now-Str); level = $level; msg = $msg })
    if ($J.logs.Count -gt 3000) { $J.logs.RemoveRange(0, 1000) }
    if ($J.echo) {
        $tag = switch ($level) { 'error' { '错误' } 'warn' { '注意' } 'ok' { '完成' } default { '信息' } }
        Write-Host ('[' + $tag + '] ' + $msg)
    }
}

function Job-Finish([string]$state, [string]$msg, [string]$level) {
    $J = $script:J
    $J.state = $state; $J.message = $msg; $J.finished = Get-Date
    if ($msg) { Job-Log $level $msg }
    if ($null -ne $J.writer) { try { $J.writer.Dispose() } catch { }; $J.writer = $null }
}

function Job-Start($cfg, [string]$mode, [bool]$echo) {
    $now = Get-Date
    $script:J = @{
        cfg = $cfg; mode = $mode; echo = $echo; state = 'running'; phase = '准备'; message = ''
        started = $now; finished = $null; total = 0; checked = 0; matched = 0; processed = 0; copied = 0; skipped = 0; failed = 0
        bytesTotal = [int64]0; bytesDone = [int64]0; cameras = (New-List); logs = (New-List); logSeq = 0
        items = (New-List); itemsTruncated = $false; warnings = (New-List); logFile = ''; targets = (New-List); timeDesc = ''
        cancel = $false; writer = $null; stage = 'filter'; idx = 0
    }
    $J = $script:J
    try {
        $F = New-Filters $cfg $now
        $J.F = $F
        $J.timeDesc = if ($F.time.enabled) { $F.time.desc } else { '不限时间' }
        $J.field = [string](Cfg $cfg 'time.field' 'created')
        $J.fallback = [string](Cfg $cfg 'time.exifFallback' 'modified')
        $tpl = [string](Cfg $cfg 'targetTemplate' '{camera}')
        $J.limitOn = [bool](Cfg $cfg 'limit.enabled' $false)
        $J.needTime = $F.time.enabled -or $J.limitOn -or $tpl.Contains('{fileDate}') -or $tpl.Contains('{fileHour}')
        if ($J.needTime -and $J.field -eq 'created' -and -not ($script:OnWin -or $script:OnMac)) {
            $J.warnings.Add('当前系统无法读取文件创建时间，已改用修改时间。'); Job-Log 'warn' $J.warnings[0]
        }
        $J.phase = '查找机位'
        $dc = Discover-Cameras $cfg $true
        foreach ($e in $dc.errors) { Job-Log 'error' $e }
        $cams = New-List
        foreach ($c in $dc.cams) { if ($c.enabled) { $cams.Add($c) } }
        if ($cams.Count -eq 0) {
            $extra = if ($dc.errors.Count -gt 0) { '：' + $dc.errors[0] } else { '' }
            UErr ('没有可处理的机位文件夹' + $extra)
        }
        $J.cams = $cams
        $tset = New-Object 'System.Collections.Generic.SortedSet[string]'
        foreach ($c in $cams) {
            $J.cameras.Add([ordered]@{ name = $c.name; source = $c.source; path = $c.path; target = ''; key = $c.key; candidates = 0; matched = 0; matchedBytes = [int64]0; copied = 0; skipped = 0; failed = 0; tMin = ''; tMax = '' })
            [void]$tset.Add($c.targetRoot)
        }
        foreach ($t in $tset) { $J.targets.Add($t) }
        Job-Log 'info' ('共 ' + $cams.Count + ' 个机位，时间条件：' + $J.timeDesc + '，依据：' + $(if ($J.needTime) { $script:FieldLabel[$J.field] } else { '—' }))

        [void][IO.Directory]::CreateDirectory($script:LogDir)
        $J.logFile = [IO.Path]::Combine($script:LogDir, $(if ($mode -eq 'preview') { 'preview_' } else { 'run_' }) + $now.ToString('yyyyMMdd_HHmmss', $script:Inv) + '.csv')
        $J.writer = New-Object IO.StreamWriter($J.logFile, $false, (New-Object Text.UTF8Encoding($true)))
        $J.writer.WriteLine((Csv-Line @('时间', '机位', '源文件', '目标文件', '结果', '说明', '文件时间', '时间来源', '大小(字节)')))

        $J.phase = '扫描文件'
        $J.cand = New-List
        $onErr = { param($m) Job-Log 'warn' $m }
        for ($ci = 0; $ci -lt $cams.Count; $ci++) {
            $files = Get-CamFiles $cams[$ci].path ([bool](Cfg $cfg 'recursive' $true)) $F.skipHidden $onErr
            foreach ($x in $files) { $x.ci = $ci; $J.cand.Add($x) }
            $J.cameras[$ci].candidates = $files.Count
        }
        $J.total = $J.cand.Count
        $J.perCam = @(); for ($ci = 0; $ci -lt $cams.Count; $ci++) { $J.perCam += , (New-List) }
        $J.tmin = New-Object 'object[]' $cams.Count; $J.tmax = New-Object 'object[]' $cams.Count
        Job-Log 'info' ('扫描完成，共 ' + $J.total + ' 个文件，开始筛选')
        $J.phase = '筛选文件'
    }
    catch {
        $level = 'error'
        Job-Finish 'error' $(if (Is-UserErr $_) { Err-Msg $_ } else { '运行出错：' + (Err-Msg $_) }) $level
    }
}

function Add-Item($it, [string]$result, [string]$note, [string]$dest) {
    $J = $script:J
    $row = [ordered]@{ camera = $J.cams[$it.ci].name; rel = $it.rel; src = $it.src; dest = $dest; time = (Fmt-Dt $it.dt); tsrc = $it.tsrc; size = $it.size; result = $result; note = $note }
    $lbl = $script:ResultLabel[$result]; if (-not $lbl) { $lbl = $result }
    $J.writer.WriteLine((Csv-Line @((Get-Date).ToString('yyyy-MM-dd HH:mm:ss', $script:Inv), $row.camera, $row.src, $dest, $lbl, $note, $row.time, $row.tsrc, $row.size)))
    if ($J.items.Count -lt 5000) { $J.items.Add($row) } else { $J.itemsTruncated = $true }
}

function Finish-Filter {
    $J = $script:J; $cfg = $J.cfg
    for ($ci = 0; $ci -lt $J.cams.Count; $ci++) { $J.cameras[$ci].tMin = Fmt-Dt $J.tmin[$ci]; $J.cameras[$ci].tMax = Fmt-Dt $J.tmax[$ci] }
    if ($J.limitOn) {
        $n = [Math]::Max(0, [int](Cfg $cfg 'limit.count' 0))
        $newest = ((Cfg $cfg 'limit.order' 'newest') -eq 'newest')
        for ($ci = 0; $ci -lt $J.perCam.Count; $ci++) {
            $withT = @($J.perCam[$ci] | Where-Object { $null -ne $_.dt } | Sort-Object { $_.dt } -Descending:$newest)
            $noT = @($J.perCam[$ci] | Where-Object { $null -eq $_.dt })
            $ordered = @($withT) + @($noT)
            $keep = New-List
            foreach ($x in (@($ordered | Select-Object -First $n) | Sort-Object { $_.rel.ToLowerInvariant() })) { $keep.Add($x) }
            $J.perCam[$ci] = $keep
        }
        $J.matched = 0; foreach ($l in $J.perCam) { $J.matched += $l.Count }
    }
    $keepStruct = [bool](Cfg $cfg 'keepStructure' $true)
    $J.all = New-List
    for ($ci = 0; $ci -lt $J.cams.Count; $ci++) {
        $cam = $J.cams[$ci]; $lst = $J.perCam[$ci]
        $sum = [int64]0; foreach ($x in $lst) { $sum += $x.size }
        $J.cameras[$ci].matched = $lst.Count; $J.cameras[$ci].matchedBytes = $sum
        $J.cameras[$ci].target = Render-TargetDir $cfg $cam $null $J.started
        foreach ($it in $lst) {
            $tdir = Render-TargetDir $cfg $cam $it.dt $J.started
            $relOut = if ($keepStruct) { $it.rel } else { [IO.Path]::GetFileName($it.rel) }
            $it.dest = [IO.Path]::Combine($tdir, $relOut)
            $J.all.Add($it)
            $J.bytesTotal += $it.size
        }
    }
    foreach ($c in $J.cameras) {
        $range = if ($c.tMin) { '，候选时间范围 ' + $c.tMin + ' ~ ' + $c.tMax } else { '' }
        Job-Log 'info' ('机位【' + $c.name + '】候选 ' + $c.candidates + '，匹配 ' + $c.matched + $range)
    }
    if ($J.mode -eq 'preview') {
        $conflict = [string](Cfg $cfg 'conflict' 'rename')
        foreach ($it in $J.all) { $pc = Predict-Conflict $it $it.dest $conflict; Add-Item $it $pc[0] $pc[1] $it.dest }
        Job-Finish 'done' ('预览完成：' + $J.cams.Count + ' 个机位中匹配 ' + $J.matched + ' 个文件（共 ' + $J.total + ' 个候选）') 'ok'
        return
    }
    $J.phase = $(if ((Cfg $cfg 'action' 'copy') -eq 'move') { '移动文件' } else { '拷贝文件' })
    $J.stage = 'copy'; $J.idx = 0
}

function Job-Step([int]$budgetMs) {
    $J = $script:J
    if ($null -eq $J -or $J.state -ne 'running') { return }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($sw.ElapsedMilliseconds -lt $budgetMs) {
            if ($J.cancel) {
                Job-Finish 'cancelled' ('已停止（已处理 ' + $J.processed + ' 个文件）') 'warn'
                return
            }
            if ($J.stage -eq 'filter') {
                if ($J.idx -ge $J.cand.Count) { Finish-Filter; if ($J.state -ne 'running') { return }; continue }
                $c = $J.cand[$J.idx]; $J.idx++; $J.checked++
                $fi = $c.fi
                if (-not (Pre-Match $J.F $fi.Name $fi.Length)) { continue }
                $dt = $null; $tsrc = ''
                if ($J.needTime) {
                    $r = Get-FileTime $fi $J.field $J.fallback
                    $dt = $r.dt; $tsrc = $r.src
                    if ($null -ne $dt) {
                        if ($null -eq $J.tmin[$c.ci] -or $dt -lt $J.tmin[$c.ci]) { $J.tmin[$c.ci] = $dt }
                        if ($null -eq $J.tmax[$c.ci] -or $dt -gt $J.tmax[$c.ci]) { $J.tmax[$c.ci] = $dt }
                    }
                }
                if ($J.F.time.enabled -and -not (Time-Match $J.F.time $dt)) { continue }
                $J.perCam[$c.ci].Add(@{ ci = $c.ci; rel = $c.rel; src = $c.full; size = [int64]$fi.Length; dt = $dt; tsrc = $tsrc; mtime = $fi.LastWriteTime; ctime = $fi.CreationTime; dest = '' })
                $J.matched++
            }
            else {
                if ($J.idx -ge $J.all.Count) {
                    $verb = if ((Cfg $J.cfg 'action' 'copy') -eq 'move') { '移动' } else { '拷贝' }
                    Job-Finish 'done' ('完成：' + $verb + ' ' + $J.copied + ' 个，跳过 ' + $J.skipped + ' 个，失败 ' + $J.failed + ' 个') $(if ($J.failed -gt 0) { 'warn' } else { 'ok' })
                    return
                }
                $it = $J.all[$J.idx]; $J.idx++
                $cs = $J.cameras[$it.ci]
                try { $res = Transfer $it $it.dest $J.cfg }
                catch { $res = @{ result = 'failed'; note = (Err-Msg $_); dest = $it.dest } }
                switch ($res.result) {
                    'skipped' { $J.skipped++; $cs.skipped++; $J.bytesDone += $it.size }
                    'failed' { $J.failed++; $cs.failed++ }
                    default { $J.copied++; $cs.copied++ }
                }
                $J.processed++
                Add-Item $it $res.result $res.note $res.dest
                $J.writer.Flush()
                $lvl = switch ($res.result) { 'failed' { 'error' } 'skipped' { 'warn' } default { 'file' } }
                $lbl = $script:ResultLabel[$res.result]
                Job-Log $lvl ('[' + $cs.name + '] ' + $it.rel + ' → ' + $lbl + $(if ($res.note) { '（' + $res.note + '）' } else { '' }))
            }
        }
    }
    catch {
        Job-Finish 'error' ('运行出错：' + (Err-Msg $_)) 'error'
    }
}

function Job-Snapshot([int]$since, [bool]$withItems) {
    $J = $script:J
    if ($null -eq $J) { return [ordered]@{ state = 'idle'; logs = @(); logSeq = 0 } }
    $logs = New-List
    foreach ($l in $J.logs) { if ($l.seq -gt $since) { $logs.Add($l) } }
    $end = if ($null -ne $J.finished) { $J.finished } else { Get-Date }
    $cams = New-List; foreach ($c in $J.cameras) { $cams.Add($c) }
    $items = $null
    if ($withItems) { $items = $J.items }
    return [ordered]@{
        state = $J.state; mode = $J.mode; phase = $J.phase; message = $J.message
        startedAt = (Fmt-Dt $J.started); finishedAt = (Fmt-Dt $J.finished); elapsed = [Math]::Round(($end - $J.started).TotalSeconds, 1)
        total = $J.total; checked = $J.checked; matched = $J.matched; processed = $J.processed; copied = $J.copied; skipped = $J.skipped; failed = $J.failed
        bytesTotal = $J.bytesTotal; bytesDone = $J.bytesDone; cameras = $cams; logs = $logs; logSeq = $J.logSeq
        items = $items; itemsTruncated = $J.itemsTruncated
        warnings = $J.warnings; logFile = $J.logFile; targets = $J.targets; timeDesc = $J.timeDesc
    }
}

# ---------------------------------------------------------------- 接口
function Api-ListRoots {
    $roots = New-List
    if ($script:OnWin) {
        foreach ($d in [Environment]::GetLogicalDrives()) { $roots.Add([ordered]@{ name = $d.Substring(0, 2) + ' 盘'; path = $d }) }
        $special = @(@('主目录', 'UserProfile'), @('桌面', 'Desktop'), @('图片', 'MyPictures'))
        foreach ($s in $special) { $p = [Environment]::GetFolderPath($s[1]); if ($p -and [IO.Directory]::Exists($p)) { $roots.Add([ordered]@{ name = $s[0]; path = $p }) } }
        $dl = [IO.Path]::Combine([Environment]::GetFolderPath('UserProfile'), 'Downloads')
        if ([IO.Directory]::Exists($dl)) { $roots.Add([ordered]@{ name = '下载'; path = $dl }) }
    }
    else {
        $roots.Add([ordered]@{ name = '根目录 /'; path = '/' })
        $bases = if ($script:OnMac) { @('/Volumes') } else { @('/media', '/mnt', '/run/media') }
        foreach ($b in $bases) {
            if ([IO.Directory]::Exists($b)) { foreach ($d in ([IO.Directory]::GetDirectories($b) | Sort-Object)) { $roots.Add([ordered]@{ name = '磁盘 ' + [IO.Path]::GetFileName($d); path = $d }) } }
        }
        $h = [Environment]::GetFolderPath('UserProfile')
        foreach ($s in @(@('主目录', ''), @('桌面', 'Desktop'), @('图片', 'Pictures'), @('下载', 'Downloads'))) {
            $p = if ($s[1]) { [IO.Path]::Combine($h, $s[1]) } else { $h }
            if ([IO.Directory]::Exists($p)) { $roots.Add([ordered]@{ name = $s[0]; path = $p }) }
        }
    }
    return , $roots
}

function Api-ListDir($d) {
    $raw = ([string](Cfg $d 'path' '')).Trim()
    $roots = Api-ListRoots
    if (-not $raw) { return [ordered]@{ path = ''; parent = $null; dirs = @(); roots = $roots } }
    try { $p = Norm-Path $raw } catch { UErr ('路径格式不正确：' + $raw) }
    if (-not [IO.Directory]::Exists($p)) { UErr ('文件夹不存在或无法访问：' + $p) }
    $dirs = New-List
    try {
        foreach ($x in ((New-Object IO.DirectoryInfo($p)).GetDirectories() | Sort-Object { $_.Name.ToLowerInvariant() })) {
            if (-not (Is-HiddenItem $x)) { $dirs.Add([ordered]@{ name = $x.Name; path = $x.FullName }) }
        }
    }
    catch { UErr ('无法读取文件夹：' + (Err-Msg $_)) }
    $parent = ''
    $pd = [IO.Directory]::GetParent($p)
    if ($null -ne $pd) { $parent = $pd.FullName }
    return [ordered]@{ path = $p; parent = $parent; dirs = $dirs; roots = $roots }
}

function Api-PickDir($d) {
    if (-not $script:OnWin) {
        if ($script:OnMac) {
            try {
                $out = & osascript -e 'activate' -e 'POSIX path of (choose folder with prompt "选择文件夹")' 2>$null
                if ($out) { return [ordered]@{ path = (Norm-Path ([string]$out)) } }
                return [ordered]@{ cancelled = $true }
            }
            catch { return [ordered]@{ unsupported = $true } }
        }
        return [ordered]@{ unsupported = $true }
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = [string](Cfg $d 'title' '选择文件夹')
        $dlg.ShowNewFolderButton = $true
        $init = [string](Cfg $d 'initial' '')
        if ($init -and [IO.Directory]::Exists($init)) { $dlg.SelectedPath = $init }
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true; $owner.ShowInTaskbar = $false
        $r = $dlg.ShowDialog($owner)
        $owner.Dispose()
        if ($r -eq [System.Windows.Forms.DialogResult]::OK) { return [ordered]@{ path = $dlg.SelectedPath } }
        return [ordered]@{ cancelled = $true }
    }
    catch { return [ordered]@{ unsupported = $true; error = (Err-Msg $_) } }
}

function Api-OpenPath($d) {
    $p = Norm-Path ([string](Cfg $d 'path' ''))
    if (-not $p -or -not (Test-Path -LiteralPath $p)) { UErr ('路径不存在：' + $p) }
    if ($script:OnWin) { Invoke-Item -LiteralPath $p }
    elseif ($script:OnMac) { & open $p }
    else { Start-Process xdg-open -ArgumentList $p }
    return [ordered]@{ ok = $true }
}

function Api-Scan($d) {
    $cfg = Merge-Defaults (Cfg $d 'config' $null) (Load-Defaults)
    $cfg.time = [PSCustomObject]@{ enabled = $false }
    $F = New-Filters $cfg (Get-Date)
    $dc = Discover-Cameras $cfg $false
    $out = New-List
    $ov = Cfg $cfg 'cameraOverrides' $null
    foreach ($c in $dc.cams) {
        $files = Get-CamFiles $c.path ([bool](Cfg $cfg 'recursive' $true)) $F.skipHidden $null
        $n = 0; $size = [int64]0
        foreach ($x in $files) { if (Pre-Match $F $x.fi.Name $x.fi.Length) { $n++; $size += $x.fi.Length } }
        $tn = ''
        if ($null -ne $ov) { $p = $ov.PSObject.Properties[$c.key]; if ($null -ne $p) { $tn = [string](Cfg $p.Value 'target' '') } }
        $out.Add([ordered]@{ pair = $c.pair; source = $c.source; name = $c.name; path = $c.path; key = $c.key; files = $n; bytes = $size; enabled = $c.enabled; target = $tn })
    }
    return [ordered]@{ cameras = $out; errors = $dc.errors }
}

function Preset-FileName([string]$name) {
    $s = (Sanitize ($name -replace '[\\/]', '_')).Trim()
    if (-not $s) { UErr '方案名称不能为空' }
    if ($s.Length -gt 80) { $s = $s.Substring(0, 80) }
    return $s + '.json'
}

function Check-PresetFile([string]$fn) {
    if (-not $fn -or $fn.Contains('/') -or $fn.Contains('\') -or $fn.Contains('..')) { UErr '非法文件名' }
    $p = [IO.Path]::Combine($script:PresetDir, $fn)
    return $p
}

function Api-PresetsList($d) {
    [void][IO.Directory]::CreateDirectory($script:PresetDir)
    $out = New-List
    foreach ($f in ([IO.Directory]::GetFiles($script:PresetDir, '*.json') | Sort-Object)) {
        $fn = [IO.Path]::GetFileName($f)
        try {
            $o = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $nm = [string](Cfg $o 'name' ''); if (-not $nm) { $nm = $fn.Substring(0, $fn.Length - 5) }
            $out.Add([ordered]@{ file = $fn; name = $nm; savedAt = [string](Cfg $o 'savedAt' '') })
        }
        catch { $out.Add([ordered]@{ file = $fn; name = $fn.Substring(0, $fn.Length - 5) + '（文件损坏）'; savedAt = '' }) }
    }
    return [ordered]@{ presets = $out }
}

function Api-PresetsSave($d) {
    $name = ([string](Cfg $d 'name' '')).Trim()
    $fn = [string](Cfg $d 'file' '')
    if (-not $fn) { $fn = Preset-FileName $name }
    $p = Check-PresetFile $fn
    [void][IO.Directory]::CreateDirectory($script:PresetDir)
    if (-not $name) { $name = $fn.Substring(0, $fn.Length - 5) }
    $obj = [ordered]@{ name = $name; savedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss', $script:Inv); tool = 'mcct'; version = 1; config = (Cfg $d 'config' (New-Object PSObject)) }
    [IO.File]::WriteAllText($p, (ConvertTo-Json -InputObject $obj -Depth 20), $script:Utf8)
    return [ordered]@{ file = $fn }
}

function Api-PresetsLoad($d) {
    $p = Check-PresetFile ([string](Cfg $d 'file' ''))
    if (-not [IO.File]::Exists($p)) { UErr '方案不存在' }
    $o = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $c = Cfg $o 'config' $null
    if ($null -eq $c) { $c = $o }
    return [ordered]@{ name = [string](Cfg $o 'name' ''); config = $c }
}

function Api-PresetsDelete($d) {
    $p = Check-PresetFile ([string](Cfg $d 'file' ''))
    if (-not [IO.File]::Exists($p)) { UErr '方案不存在' }
    [IO.File]::Delete($p)
    return [ordered]@{ ok = $true }
}

function Api-Start($d) {
    if ($null -ne $script:J -and $script:J.state -eq 'running') { UErr '已有任务正在运行' }
    $mode = if ((Cfg $d 'mode' '') -eq 'preview') { 'preview' } else { 'run' }
    $cfg = Merge-Defaults (Cfg $d 'config' $null) (Load-Defaults)
    [void](New-Filters $cfg (Get-Date))
    Job-Start $cfg $mode $false
    return [ordered]@{ ok = $true }
}

function Invoke-Api([string]$name, $d) {
    switch ($name) {
        'info' {
            $i = System-Info
            $i.defaults = ($script:DefaultsJson | ConvertFrom-Json)
            return $i
        }
        'list_dir' { return (Api-ListDir $d) }
        'pick_dir' { return (Api-PickDir $d) }
        'open_path' { return (Api-OpenPath $d) }
        'scan' { return (Api-Scan $d) }
        'start' { return (Api-Start $d) }
        'status' { return (Job-Snapshot ([int](Cfg $d 'since' 0)) ([bool](Cfg $d 'withItems' $false))) }
        'cancel' { if ($null -ne $script:J -and $script:J.state -eq 'running') { $script:J.cancel = $true }; return [ordered]@{ ok = $true } }
        'presets_list' { return (Api-PresetsList $d) }
        'presets_save' { return (Api-PresetsSave $d) }
        'presets_load' { return (Api-PresetsLoad $d) }
        'presets_delete' { return (Api-PresetsDelete $d) }
        'shutdown' { if ($null -ne $script:J -and $script:J.state -eq 'running') { $script:J.cancel = $true; Job-Step 50 }; $script:Quit = $true; return [ordered]@{ ok = $true } }
    }
    return $null
}

# ---------------------------------------------------------------- HTTP（单线程、非阻塞轮询，任务在空闲时分步执行）
function Send-Response($conn, [int]$code, [byte[]]$body, [string]$ctype) {
    $reasons = @{ 200 = 'OK'; 204 = 'No Content'; 400 = 'Bad Request'; 403 = 'Forbidden'; 404 = 'Not Found'; 500 = 'Internal Server Error' }
    if ($null -eq $body) { $body = New-Object byte[] 0 }
    $hdr = 'HTTP/1.1 ' + $code + ' ' + $reasons[$code] + "`r`nContent-Type: " + $ctype + "`r`nContent-Length: " + $body.Length + "`r`nCache-Control: no-store`r`nX-Content-Type-Options: nosniff`r`nConnection: close`r`n`r`n"
    $hb = [Text.Encoding]::ASCII.GetBytes($hdr)
    try {
        $conn.s.Write($hb, 0, $hb.Length)
        if ($body.Length -gt 0) { $conn.s.Write($body, 0, $body.Length) }
        $conn.s.Flush()
    }
    catch { }
}

function Send-Json($conn, [int]$code, $obj) {
    $json = ConvertTo-Json -InputObject $obj -Depth 12 -Compress
    Send-Response $conn $code ($script:Utf8.GetBytes($json)) 'application/json; charset=utf-8'
}

function Try-Handle($conn) {
    $bytes = $conn.buf.ToArray()
    $text = $script:Latin1.GetString($bytes)
    $he = $text.IndexOf("`r`n`r`n", [StringComparison]::Ordinal)
    if ($he -lt 0) { return $false }
    $lines = $text.Substring(0, $he).Split(@("`r`n"), [StringSplitOptions]::None)
    $req = $lines[0].Split(' ')
    $headers = @{}
    for ($i = 1; $i -lt $lines.Length; $i++) { $k = $lines[$i].IndexOf(':'); if ($k -gt 0) { $headers[$lines[$i].Substring(0, $k).Trim()] = $lines[$i].Substring($k + 1).Trim() } }
    $cl = 0; if ($headers.ContainsKey('Content-Length')) { $cl = [int]$headers['Content-Length'] }
    if ($bytes.Length -lt $he + 4 + $cl) { return $false }
    $method = $req[0]; $path = if ($req.Length -gt 1) { $req[1] } else { '/' }
    $q = $path.IndexOf('?'); if ($q -ge 0) { $path = $path.Substring(0, $q) }

    if ($method -eq 'GET') {
        if ($path -eq '/' -or $path -eq '/index.html') {
            if ([IO.File]::Exists($script:AppFile)) { Send-Response $conn 200 ([IO.File]::ReadAllBytes($script:AppFile)) 'text/html; charset=utf-8' }
            else { Send-Response $conn 500 ($script:Utf8.GetBytes('找不到 app/index.html')) 'text/plain; charset=utf-8' }
        }
        elseif ($path -eq '/favicon.ico') { Send-Response $conn 204 $null 'image/x-icon' }
        else { Send-Response $conn 404 ([Text.Encoding]::ASCII.GetBytes('Not Found')) 'text/plain' }
        return $true
    }
    if ($method -ne 'POST' -or -not $path.StartsWith('/api/')) { Send-Json $conn 404 @{ error = 'not found' }; return $true }
    if ($headers['X-Token'] -ne $script:Token) { Send-Json $conn 403 @{ error = '访问令牌无效，请通过启动脚本重新打开控制台' }; return $true }
    $data = $null
    try {
        $bodyText = if ($cl -gt 0) { [Text.Encoding]::UTF8.GetString($bytes, $he + 4, $cl) } else { '{}' }
        if ($bodyText.Trim()) { $data = $bodyText | ConvertFrom-Json } else { $data = New-Object PSObject }
    }
    catch { Send-Json $conn 400 @{ error = '请求格式错误' }; return $true }
    try {
        $res = Invoke-Api $path.Substring(5) $data
        if ($null -eq $res) { Send-Json $conn 404 @{ error = '未知接口' } }
        else { Send-Json $conn 200 $res }
    }
    catch {
        if (Is-UserErr $_) { Send-Json $conn 400 @{ error = (Err-Msg $_) } }
        else { Write-Host ('[内部错误] ' + $_.ToString() + ' @ ' + $_.InvocationInfo.PositionMessage); Send-Json $conn 500 @{ error = '内部错误：' + (Err-Msg $_) } }
    }
    return $true
}

function Serve {
    $listener = $null
    for ($p = $Port; $p -lt $Port + 40; $p++) {
        try { $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $p); $listener.Start(); $script:Port = $p; break }
        catch { $listener = $null }
    }
    if ($null -eq $listener) { Write-Host '找不到可用端口'; return }
    $url = 'http://127.0.0.1:' + $script:Port + '/?t=' + $script:Token
    $info = System-Info
    try { $Host.UI.RawUI.WindowTitle = '多机位素材筛选拷贝工具 - 请勿关闭' } catch { }
    Write-Host ('=' * 60)
    Write-Host (' 多机位素材筛选拷贝工具 v' + $script:ToolVersion + '  (PowerShell ' + $PSVersionTable.PSVersion + ' 引擎)')
    Write-Host (' 系统：' + $info.osLabel)
    Write-Host (' 控制台地址：' + $url)
    Write-Host ' 使用期间请不要关闭本窗口；关闭窗口即退出程序。'
    Write-Host ('=' * 60)
    if (-not $NoBrowser) { try { Start-Process $url } catch { Write-Host '请手动在浏览器中打开上面的地址。' } }

    $conns = New-Object 'System.Collections.Generic.List[object]'
    $tmp = New-Object byte[] 65536
    while (-not $script:Quit) {
        $busy = $false
        while ($listener.Pending()) {
            $c = $listener.AcceptTcpClient()
            $conns.Add(@{ c = $c; s = $c.GetStream(); buf = (New-Object IO.MemoryStream); sw = [Diagnostics.Stopwatch]::StartNew() })
            $busy = $true
        }
        for ($i = $conns.Count - 1; $i -ge 0; $i--) {
            $conn = $conns[$i]
            $done = $false
            try {
                while ($conn.c.Available -gt 0) {
                    $n = $conn.s.Read($tmp, 0, $tmp.Length)
                    if ($n -le 0) { break }
                    $conn.buf.Write($tmp, 0, $n); $busy = $true
                }
                if ($conn.buf.Length -gt 0) { $done = Try-Handle $conn }
                if (-not $done -and $conn.sw.ElapsedMilliseconds -gt 15000) { $done = $true }
            }
            catch { $done = $true }
            if ($done) { try { $conn.c.Close() } catch { }; $conns.RemoveAt($i) }
        }
        if ($null -ne $script:J -and $script:J.state -eq 'running') { Job-Step 120 }
        elseif (-not $busy) { Start-Sleep -Milliseconds 15 }
    }
    foreach ($conn in $conns) { try { $conn.c.Close() } catch { } }
    $listener.Stop()
    Write-Host '程序已退出。'
}

function Run-Cli([string]$file) {
    $o = [IO.File]::ReadAllText((Norm-Path $file), [Text.Encoding]::UTF8) | ConvertFrom-Json
    $c = Cfg $o 'config' $null
    if ($null -eq $c) { $c = $o }
    $cfg = Merge-Defaults $c (Load-Defaults)
    Job-Start $cfg $(if ($Preview) { 'preview' } else { 'run' }) $true
    while ($script:J.state -eq 'running') { Job-Step 1000 }
    Write-Host ('日志文件：' + $script:J.logFile)
    if ($script:J.state -ne 'done') { return 1 }
    if ($script:J.failed -gt 0) { return 2 }
    return 0
}

if ($Run) { exit (Run-Cli $Run) }
Serve
