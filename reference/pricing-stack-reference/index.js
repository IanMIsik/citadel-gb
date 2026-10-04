/* eslint-disable no-inner-declarations */
const _ = require('lodash');
const path = require('path');
const { ipcRenderer } = require('electron');

const generatePricingStackTable = require(path.resolve(
  __dirname,
  '../components/tables/view-utils'
));
let allSps = [];
let pricingStackTableData = JSON.parse(
  localStorage.getItem('delta_pricing_by_sp')
);
let pricingStackInitialData = {
  delta_pricing_by_sp: pricingStackTableData
};
generatePricingStackComponents(pricingStackInitialData);

ipcRenderer.on('reload', (event, data) => {
  //reloads the main window when the pricing stack window is reloaded
  ipcRenderer.send('reload');
});

ipcRenderer.on('check-updated-cache', (e, data) => {
  pricingStackTableData = JSON.parse(
    localStorage.getItem('delta_pricing_by_sp')
  );
  pricingStackInitialData = {
    delta_pricing_by_sp: pricingStackTableData
  };
  generatePricingStackComponents(pricingStackInitialData);
});


function generatePricingStackComponents(data) {
  if (data && data['delta_pricing_by_sp']) {
    let pricingData = data['delta_pricing_by_sp'];
    let firstSpActions = [];
    let orderedSps = [];
    let pricingStackArray = Object.values(pricingData);
    for (let index = 0; index < pricingStackArray.length; index++) {
      //get the first action of every sp
      //fixes the midnight bug that transitions from 48 to 1
      const element = pricingStackArray[index];
      for (let index = 0; index < element.length; index++) {
        if (index == 0) {
          //get the first action of the array
          const value = element[index];
          firstSpActions.push(value);
          break;
        }
      }
    }
    //sortby the date so that we always get the latest sp on top of the selection
    firstSpActions = _.sortBy(firstSpActions, ['spot_time_uk']);
    firstSpActions.forEach(value => {
      orderedSps.push(value['sp']);
    });
    if (orderedSps.length >= 4) {
      allSps = [...orderedSps.reverse()];
    }
    generatePricingStackTable(pricingData, allSps);
  }
}
