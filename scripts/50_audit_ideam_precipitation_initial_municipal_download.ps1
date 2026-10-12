<#!
Audits the initial IDEAM precipitation CSVs without changing the raw files.
Outputs one compact JSON record per input file plus cross-file duplicate checks.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$rawDir = Join-Path $root 'data\raw\ideam'
$output = Join-Path $root 'results\exploratory\audit_ideam_precipitation_initial_municipal_download.json'
$files = Get-ChildItem -LiteralPath $rawDir -File -Filter 'Precipitacion*.csv' | Sort-Object Name

if ($files.Count -eq 0) {
    throw 'No precipitation CSV files found in data/raw/ideam.'
}

$allKeys = [System.Collections.Generic.List[string]]::new()
$summaries = foreach ($file in $files) {
    $rows = @(Import-Csv -LiteralPath $file.FullName)
    $parsed = foreach ($row in $rows) {
        [datetime]$date = [datetime]::MinValue
        $dateOk = [datetime]::TryParseExact(
            $row.Fecha,
            'yyyy-MM-dd HH:mm',
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::None,
            [ref]$date
        )
        [double]$value = 0
        $valueOk = [double]::TryParse(
            $row.Valor,
            [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [ref]$value
        )
        [pscustomobject]@{
            Row = $row
            Date = $date
            DateOk = $dateOk
            Value = $value
            ValueOk = $valueOk
        }
    }
    $keys = @($parsed | Where-Object { $_.DateOk } | ForEach-Object { "{0}|{1}" -f $_.Row.CodigoEstacion, $_.Date.ToString('yyyy-MM-ddTHH:mm') })
    foreach ($key in $keys) { $allKeys.Add($key) }
    $august = @($parsed | Where-Object { $_.DateOk -and $_.Date -ge [datetime]'2026-08-01' -and $_.Date -lt [datetime]'2026-09-01' })
    $validValues = @($parsed | Where-Object { $_.ValueOk } | ForEach-Object Value)
    $stationSummaries = foreach ($stationCode in @($rows.CodigoEstacion | Sort-Object -Unique)) {
        $stationRows = @($parsed | Where-Object { $_.Row.CodigoEstacion -eq $stationCode })
        $stationAugust = @($stationRows | Where-Object { $_.DateOk -and $_.Date -ge [datetime]'2026-08-01' -and $_.Date -lt [datetime]'2026-09-01' })
        [ordered]@{
            station_code = $stationCode
            station_name = @($stationRows | ForEach-Object { $_.Row.NombreEstacion } | Sort-Object -Unique)
            row_count = $stationRows.Count
            first_date = if (@($stationRows | Where-Object DateOk).Count -gt 0) { ($stationRows | Where-Object DateOk | Measure-Object Date -Minimum).Minimum.ToString('yyyy-MM-dd') } else { $null }
            last_date = if (@($stationRows | Where-Object DateOk).Count -gt 0) { ($stationRows | Where-Object DateOk | Measure-Object Date -Maximum).Maximum.ToString('yyyy-MM-dd') } else { $null }
            august_2026_row_count = $stationAugust.Count
            august_2026_unique_dates = @($stationAugust | ForEach-Object { $_.Date.ToString('yyyy-MM-dd') } | Sort-Object -Unique).Count
            august_2026_first_date = if ($stationAugust.Count -gt 0) { ($stationAugust | Measure-Object Date -Minimum).Minimum.ToString('yyyy-MM-dd') } else { $null }
            august_2026_last_date = if ($stationAugust.Count -gt 0) { ($stationAugust | Measure-Object Date -Maximum).Maximum.ToString('yyyy-MM-dd') } else { $null }
            august_2026_approval_levels = @($stationAugust | ForEach-Object { $_.Row.NivelAprobacion } | Sort-Object -Unique)
        }
    }
    [ordered]@{
        file = $file.Name
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        row_count = $rows.Count
        columns = @($rows[0].PSObject.Properties.Name)
        station_codes = @($rows.CodigoEstacion | Sort-Object -Unique)
        station_names = @($rows.NombreEstacion | Sort-Object -Unique)
        variables = @($rows.Variable | Sort-Object -Unique)
        parameters = @($rows.Parametro | Sort-Object -Unique)
        units = @($rows.Unidad | Sort-Object -Unique)
        approval_levels = @($rows.NivelAprobacion | Sort-Object -Unique)
        invalid_date_rows = @($parsed | Where-Object { -not $_.DateOk }).Count
        invalid_value_rows = @($parsed | Where-Object { -not $_.ValueOk }).Count
        within_file_duplicate_station_datetime_rows = $keys.Count - (@($keys | Sort-Object -Unique).Count)
        first_date = if ($parsed.DateOk.Count -gt 0) { ($parsed | Where-Object DateOk | Measure-Object Date -Minimum).Minimum.ToString('yyyy-MM-dd') } else { $null }
        last_date = if ($parsed.DateOk.Count -gt 0) { ($parsed | Where-Object DateOk | Measure-Object Date -Maximum).Maximum.ToString('yyyy-MM-dd') } else { $null }
        minimum_numeric_value = if ($validValues.Count -gt 0) { ($validValues | Measure-Object -Minimum).Minimum } else { $null }
        maximum_numeric_value = if ($validValues.Count -gt 0) { ($validValues | Measure-Object -Maximum).Maximum } else { $null }
        stations = @($stationSummaries)
        august_2026_row_count = $august.Count
        august_2026_unique_dates = @($august | ForEach-Object { $_.Date.ToString('yyyy-MM-dd') } | Sort-Object -Unique).Count
        august_2026_first_date = if ($august.Count -gt 0) { ($august | Measure-Object Date -Minimum).Minimum.ToString('yyyy-MM-dd') } else { $null }
        august_2026_last_date = if ($august.Count -gt 0) { ($august | Measure-Object Date -Maximum).Maximum.ToString('yyyy-MM-dd') } else { $null }
        august_2026_preliminary_rows = @($august | Where-Object { $_.Row.NivelAprobacion -eq 'Preliminar' }).Count
    }
}

$result = [ordered]@{
    audited_at_local = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    raw_directory = 'data/raw/ideam'
    files = @($summaries)
    cross_file_duplicate_station_datetime_rows = $allKeys.Count - (@($allKeys | Sort-Object -Unique).Count)
    interpretation_limit = 'This audit verifies the delivered CSV structure and temporal coverage only. It does not verify station coordinates, municipal location, measurement method, gauge calibration, missing observations not represented as rows, or representativeness beyond each named station.'
}

$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $output -Encoding utf8
Write-Output $output
$result.files | Select-Object file, row_count, station_codes, first_date, last_date, august_2026_unique_dates, august_2026_preliminary_rows | Format-Table -AutoSize
Write-Output "Cross-file duplicate station-datetime rows: $($result.cross_file_duplicate_station_datetime_rows)"
