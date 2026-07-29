FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /source

# The submodule subtree and every other non-build input are excluded by .dockerignore.
COPY global.json Directory.Build.props ./
COPY src/ ./src/

RUN dotnet restore src/ChildRepo10Loc/ChildRepo10Loc.csproj

RUN dotnet publish src/ChildRepo10Loc/ChildRepo10Loc.csproj \
    --configuration Release \
    --no-restore \
    --output /app

FROM mcr.microsoft.com/dotnet/runtime:8.0 AS final
WORKDIR /app

COPY --from=build /app .

# .NET images run as root unless USER is set; $APP_UID is the non-root app account, UID 1654.
USER $APP_UID

# Console workload with no listening socket, so no port or health directive is declared.
ENTRYPOINT ["dotnet", "ChildRepo10Loc.dll"]
