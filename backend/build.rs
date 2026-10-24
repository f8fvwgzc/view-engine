//! Generates the gRPC client for the sidecars from `proto/` (see `src/sidecar.rs`).

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // PROTO_DIR lets a container build point at its copy of proto/; locally it is the repo's.
    let root = std::env::var("PROTO_DIR").unwrap_or_else(|_| "../proto".into());
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    tonic_build::configure()
        .build_server(false)
        // Bodies stay `bytes::Bytes` end to end: no copy between the wire, the cache and Redis.
        .bytes(["."])
        .compile_protos(&[format!("{root}/viewengine/sidecar/v1/sidecar.proto")], &[root.clone()])?;
    println!("cargo:rerun-if-changed={root}");
    println!("cargo:rerun-if-env-changed=PROTO_DIR");
    Ok(())
}
