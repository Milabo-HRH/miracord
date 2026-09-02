param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [Parameter(Mandatory = $true)]
    [string]$Text
)

Add-Type -AssemblyName System.Speech
$synthesizer = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $synthesizer.SetOutputToWaveFile($OutputPath)
    $synthesizer.Speak($Text)
}
finally {
    $synthesizer.Dispose()
}
