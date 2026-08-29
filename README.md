# Woody Monitor

Woody Monitor is a local monitoring interface for NBE/Scotte/Woody pellet
burner controllers.

The project provides:

- Live burner monitoring
- Historical measurements
- Pellet consumption monitoring
- Local web interface
- SQLite data storage
- Optional MQTT integration
- Communication with supported burner controllers through the
  Scotte/PellMon protocol

## Third-party software

Woody Monitor contains and builds upon code originating from the
[PellMon project](https://github.com/motoz/PellMon).

The PellMon-derived components are identified by their original copyright
and GPL license notices.

In particular, the Scotte/NBE communication implementation contains
copyright material from Anders Nylund.

See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for additional
information.

## License

Woody Monitor is distributed under the GNU General Public License v2.0
where applicable.

Third-party source files retain their original copyright and license
notices.

See [LICENSE](LICENSE) for the full GNU General Public License v2.0 text.

## Disclaimer

Woody Monitor is an independent project.

It is not affiliated with, endorsed by, sponsored by, or maintained by
PellMon, Anders Nylund, or NBE.
