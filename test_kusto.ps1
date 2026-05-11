$token = az account get-access-token --resource https://kusto.kusto.windows.net --query accessToken -o tsv
# $token = az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv
$headers = @{ 
    Authorization = "Bearer $token"
    "Content-Type" = "application/json"
    Accept = "application/json"
}
$body = @{
    db  = "NetDefaultDB"
    csl = ".show databases"
    properties = @{ Options = @{ queryconsistency = "strongconsistency" } }
} | ConvertTo-Json -Depth 5

try {
    $result = Invoke-RestMethod `
        -Uri "https://trd-yx5sb49k5h3br7jg7c.z4.kusto.fabric.microsoft.com/v1/rest/query" `
        -Method Post -Headers $headers -Body $body
    $result.Tables[0].Rows | ForEach-Object { Write-Host $_[0] }
} catch {
    $reader = [System.IO.StreamReader]::new($_.Exception.Response.GetResponseStream())
    Write-Host "Fel:" $reader.ReadToEnd()
}