mod fault_probes;

fn main() {
    let mut args = std::env::args_os().skip(1);
    if let Some(command) = args.next() {
        if command == "--corruption-probe"
            || command == "--disk-full-probe"
            || command == "--full-text-disk-full-probe"
            || command == "--disk-fixture-probe"
        {
            if args.next().is_some() {
                eprintln!("fault probes do not accept positional arguments");
                std::process::exit(2);
            }
            let (report, exit_code) = if command == "--corruption-probe" {
                match fault_probes::corruption_probe() {
                    Ok(report) => (report, 0),
                    Err(error) => (fault_probes::failure("edge_corruption_probe", error), 1),
                }
            } else if command == "--disk-fixture-probe" {
                match fault_probes::disk_fixture_probe() {
                    Ok(report) => (report, 0),
                    Err(error) => (fault_probes::failure("edge_disk_fixture_probe", error), 1),
                }
            } else {
                fault_probes::disk_full_probe(command == "--full-text-disk-full-probe")
            };
            println!("{}", serde_json::to_string_pretty(&report).unwrap());
            std::process::exit(exit_code);
        }
        if command == "--persistence-child" {
            let Some(path) = args.next() else {
                eprintln!("--persistence-child requires an existing empty directory");
                std::process::exit(2);
            };
            let flush = match args.next() {
                Some(mode) if mode == "flushed" => true,
                Some(mode) if mode == "unflushed" => false,
                _ => {
                    eprintln!("--persistence-child requires flushed or unflushed mode");
                    std::process::exit(2);
                }
            };
            if args.next().is_some() {
                eprintln!("unexpected persistence probe argument");
                std::process::exit(2);
            }
            let _owner =
                match pg_qdrant_edge_probe::persistence_fixture(std::path::Path::new(&path), flush)
                {
                    Ok(owner) => owner,
                    Err(error) => {
                        eprintln!("{error}");
                        std::process::exit(1);
                    }
                };
            use std::io::Write;
            println!("ready");
            std::io::stdout().flush().unwrap();
            // The parent keeps stdin open until it kills this process. This
            // path never invokes PostgreSQL or opens someone else's shard.
            let mut line = String::new();
            std::io::stdin().read_line(&mut line).unwrap();
            return;
        }
        eprintln!("unknown probe option: {}", command.to_string_lossy());
        std::process::exit(2);
    }
    match pg_qdrant_edge_probe::run_smoke() {
        Ok(report) => println!("{}", serde_json::to_string_pretty(&report).unwrap()),
        Err(error) => {
            eprintln!("edge probe failed: {error}");
            std::process::exit(1);
        }
    }
}
