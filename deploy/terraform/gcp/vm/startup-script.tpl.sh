#!/bin/sh
# Amorçage k3s pour le mode par défaut Quadringent (--target vm). Ce script
# installe k3s en single-node ; l'installation de la chart Quadringent elle-
# même reste pilotée par le CLI `quadringent install` depuis le poste
# opérateur, via kubeconfig récupéré par IAP TCP forwarding.
set -eu
installer=$(mktemp)
trap 'rm -f "$installer"' EXIT HUP INT TERM
curl --fail --show-error --location --connect-timeout 10 --max-time 60 \
  --retry 3 --retry-delay 2 --retry-max-time 180 \
  --output "$installer" https://get.k3s.io
INSTALL_K3S_CHANNEL=${k3s_channel} sh "$installer" server --write-kubeconfig-mode=600 --disable=traefik
systemctl is-active --quiet k3s.service
mkdir -p /etc/quadringent
echo "site=${site_name}" > /etc/quadringent/site.env
