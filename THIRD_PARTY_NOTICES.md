# Third-party components and redistribution

Apache-2.0 covers Quadringent's original code. The dependencies below remain
separate works under their own licenses; they are not converted to Apache code.
Retain their notices in every redistribution. Third-party components are
provided without warranty; their license disclaimers and liability exclusions
apply to all their contributors. Any Quadringent support commitment is made
solely by its provider on its own behalf.

## Java

Versions are defined in `java/pom.xml`. Libraries are distributed as separate
JARs, unchanged by Quadringent. Reader and cockpit images include corresponding
source archives in `/usr/share/quadringent/sources/`, with original headers.
`java/third-party-sources.sha256` verifies these archives during build.

| Component | Version | License and attribution |
|---|---|---|
| `io.debezium:ibmi-journal-parsing` | 3.6.1.Final | Apache-2.0; Copyright Debezium Authors |
| `io.debezium:jt400-override-ccsid` | 3.6.1.Final | Apache-2.0 and portions under IPL-1.0; retain the IBM header in `AS400JDBCDriverForcedCcsid.java` |
| `net.sf.jt400:jt400` | 21.0.7 | IPL-1.0; International Business Machines Corporation and others |
| `org.slf4j:slf4j-api`, `org.slf4j:slf4j-simple` | 2.0.12 | MIT; QOS.ch and contributors |

License texts: [Apache-2.0](LICENSE), [IBM IPL-1.0](licenses/JTOpen-IPL-1.0.html),
[SLF4J MIT](licenses/SLF4J-MIT.txt). JTOpen and IBM portions remain available
under IPL-1.0 with their notices and warranty disclaimers.

Exact upstream sources are also available independently of the images:

- [ibmi-journal-parsing 3.6.1.Final](https://repo.maven.apache.org/maven2/io/debezium/ibmi-journal-parsing/3.6.1.Final/ibmi-journal-parsing-3.6.1.Final-sources.jar)
- [jt400-override-ccsid 3.6.1.Final](https://repo.maven.apache.org/maven2/io/debezium/jt400-override-ccsid/3.6.1.Final/jt400-override-ccsid-3.6.1.Final-sources.jar)
- [JTOpen 21.0.7](https://repo.maven.apache.org/maven2/net/sf/jt400/jt400/21.0.7/jt400-21.0.7-sources.jar)
- [slf4j-api 2.0.12](https://repo.maven.apache.org/maven2/org/slf4j/slf4j-api/2.0.12/slf4j-api-2.0.12-sources.jar)
- [slf4j-simple 2.0.12](https://repo.maven.apache.org/maven2/org/slf4j/slf4j-simple/2.0.12/slf4j-simple-2.0.12-sources.jar)

## Python

Dependencies are declared in `pyproject.toml` and `requirements*.txt`.
Cockpit and verifier images use pinned versions and hashes from
`docker/*-requirements.txt`. The Snowflake extra is included in this review.

| Family | Selected or applicable license |
|---|---|
| boto3, botocore, s3transfer, requests, Snowflake Connector, pyOpenSSL, sortedcontainers | Apache-2.0 |
| python-dateutil, packaging, cryptography | Apache-2.0 among alternative licenses; retain all supplied texts |
| asn1crypto, charset-normalizer, filelock, jmespath, platformdirs, PyJWT, pytz, six, tomlkit, urllib3 | MIT |
| cffi | MIT-0 for the pinned version |
| idna, pycparser | BSD-3-Clause |
| typing-extensions | PSF-2.0 |
| certifi | MPL-2.0, applying to its files; retain license and source access |

Installations retain `*.dist-info` files and package notices, including those
of components incorporated by Snowflake Connector. Images also collect these
texts in `/usr/share/quadringent/python-licenses/third-party-notices.txt`,
with versions and source-distribution links in `distributions.json`.
Distributed Python code and certifi data are readable in the image; upstream
sources are available on each indicated version's PyPI page. Certifi's MPL
does not replace the license of Quadringent files.

## Interface and build tools

`ui/package-lock.json` pins versions. React, React DOM and Scheduler are MIT.
IBM Plex Sans and IBM Plex Mono fonts are OFL-1.1, distributed unchanged with
their notices and never sold separately. The static site build copies Latin
`woff2` files from `@fontsource-variable/ibm-plex-sans` and
`@fontsource/ibm-plex-mono` 5.3.0. Corresponding OFL-1.1 texts reside in
`site/assets/fonts/`. The build assembles complete texts and Vite runtime
notices in `ui/dist/assets/third-party-notices.txt`, served at the same path
in the cockpit.

The npm build graph includes MIT, ISC, BSD-3-Clause, Apache-2.0, OFL-1.1 and
CC-BY-4.0 (`caniuse-lite`, build data). These tools are not copied wholesale
into the final image. Redistribution must retain applicable attribution.

Python development tools retain licenses in their distributions: pytest,
pluggy, iniconfig, pyflakes, PyYAML, Ruff, mypy, mypy-extensions, librt, build and
pyproject-hooks are MIT; Pygments is BSD-2-Clause; pathspec is MPL-2.0.
They are not included in the Quadringent package. Pathspec files remain MPL
without changing the license of code analyzed by the tools.

## Base images and updates

Python/Debian and Eclipse Temurin bases retain their own notices, including
`/usr/share/doc` and `/opt/java/openjdk/legal`. The JRE includes GPL and
Classpath Exception texts; retain the exception. JDK, Maven and Node remain
in build stages.

Dependency updates require a new license and notice review. Review of current
versions does not approve every future version allowed by a dependency range.
